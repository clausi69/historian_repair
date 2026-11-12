"""Core engine: access CCU-Historian H2 databases via JDBC (JPype) and merge them.

Design principles
-----------------
* Source databases are never opened in place. Their files are copied into a
  work directory first; only the copies are opened. Size and mtime of the
  originals are recorded before and checked after the operation.
* Each H2 jar is loaded into its own Java class loader, so databases of
  different H2 versions (1.4.x, 2.1.x, 2.2+) can be open at the same time.
* The JVM runs with user.timezone=UTC. H2 TIMESTAMP columns (without time
  zone) are therefore converted 1:1 to epoch milliseconds - no DST shifts.
  All "ms" values in this module are the stored wall-clock time interpreted
  as UTC.
* Reading is fault tolerant: data is read in time windows; a window that
  fails is bisected until the defective part is smaller than a configurable
  minimum. Lost intervals are reported.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import re
import shutil
import threading
import time
import urllib.request
import zipfile
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

log = logging.getLogger("historian_repair")

# ---------------------------------------------------------------------------
# Constants / helpers
# ---------------------------------------------------------------------------

H2_DOWNLOAD_VERSIONS = ["1.4.200", "2.1.214", "2.2.224", "2.3.232", "2.4.240", "2.5.252"]
MAVEN_URL = "https://repo1.maven.org/maven2/com/h2database/h2/{v}/h2-{v}.jar"

FORMAT_TEXT = {
    1: "H2 1.4.x (MVStore-Format 1)",
    2: "H2 2.0.x/2.1.x (MVStore-Format 2)",
    3: "H2 2.2.x oder neuer (MVStore-Format 3)",
}

DATA_POINTS = "DATA_POINTS"
TS_COL = "TS"
EPOCH = _dt.datetime(1970, 1, 1)
DAY_MS = 86_400_000


class Cancelled(Exception):
    """Raised when the user cancels a running operation."""


def ms_to_str(ms: Optional[int], seconds: bool = True) -> str:
    if ms is None:
        return ""
    d = EPOCH + _dt.timedelta(milliseconds=int(ms))
    return d.strftime("%Y-%m-%d %H:%M:%S" if seconds else "%Y-%m-%d %H:%M")


_DATE_FORMATS = [
    "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
    "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y",
]


def str_to_ms(text: str) -> Optional[int]:
    """Parse a user supplied wall-clock time. Empty string -> None."""
    text = (text or "").strip()
    if not text:
        return None
    for fmt in _DATE_FORMATS:
        try:
            d = _dt.datetime.strptime(text, fmt)
            return int((d - EPOCH).total_seconds() * 1000)
        except ValueError:
            continue
    raise ValueError(f"Ungültiges Datum: '{text}' (erwartet z.B. 2024-03-31 23:59:59 oder 31.03.2024 23:59)")


def jar_version(jar: str) -> Optional[str]:
    """Read the H2 version from a jar (manifest or file name)."""
    try:
        with zipfile.ZipFile(jar) as z:
            names = set(z.namelist())
            if "org/h2/Driver.class" not in names:
                return None
            try:
                mf = z.read("META-INF/MANIFEST.MF").decode("utf-8", "replace")
                for key in ("Implementation-Version", "Bundle-Version"):
                    m = re.search(rf"^{key}:\s*(\S+)", mf, re.M)
                    if m and re.match(r"\d+\.\d+", m.group(1)):
                        return m.group(1)
            except KeyError:
                pass
    except (OSError, zipfile.BadZipFile):
        return None
    m = re.search(r"h2-(\d+\.\d+\.\d+)", os.path.basename(jar))
    return m.group(1) if m else "unbekannt"


def format_for_version(version: Optional[str]) -> Optional[int]:
    """MVStore format written by a given H2 version."""
    if not version:
        return None
    m = re.match(r"(\d+)\.(\d+)", version)
    if not m:
        return None
    major, minor = int(m.group(1)), int(m.group(2))
    if major == 1:
        return 1 if minor >= 4 else None
    if major == 2:
        return 2 if minor <= 1 else 3
    return 3


def _version_tuple(v: Optional[str]) -> tuple:
    try:
        return tuple(int(x) for x in re.findall(r"\d+", v or "")[:3])
    except ValueError:
        return ()


def is_v1(version: Optional[str]) -> bool:
    return bool(version) and version.startswith("1.")


def download_h2(version: str, dest_dir: str) -> str:
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, f"h2-{version}.jar")
    tmp = dest + ".part"
    with urllib.request.urlopen(MAVEN_URL.format(v=version), timeout=60) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f)
    os.replace(tmp, dest)
    return dest


def q(ident: str) -> str:
    """Quote an SQL identifier."""
    return '"' + ident.replace('"', '""') + '"'


def historian_table_name(interface: str, address: str, identifier: str) -> str:
    """Data table name as used by CCU-Historian (D_<IF>_<ADDR>_<IDENT>)."""
    def norm(s: str) -> str:
        return re.sub(r"[^A-Z0-9]", "_", (s or "").upper())
    return f"D_{norm(interface)}_{norm(address)}_{norm(identifier)}"


# ---------------------------------------------------------------------------
# Database file inspection
# ---------------------------------------------------------------------------

@dataclass
class DbFileInfo:
    path: str
    base: str            # path without .mv.db/.h2.db (as used in the JDBC URL)
    kind: str            # "mv" or "page"
    size: int
    mtime_ns: int
    header: dict = field(default_factory=dict)
    fmt: Optional[int] = None
    lock_file: Optional[str] = None
    header_error: Optional[str] = None

    @property
    def format_text(self) -> str:
        if self.kind == "page":
            return "H2 PageStore (.h2.db, H2 1.3/1.4)"
        if self.fmt in FORMAT_TEXT:
            return FORMAT_TEXT[self.fmt]
        return "unbekannt" + (f" ({self.header_error})" if self.header_error else "")


def inspect_db_file(path: str) -> DbFileInfo:
    path = os.path.abspath(path)
    if path.endswith(".mv.db"):
        base, kind = path[:-6], "mv"
    elif path.endswith(".h2.db"):
        base, kind = path[:-6], "page"
    else:
        raise ValueError("Datei muss auf .mv.db oder .h2.db enden")
    st = os.stat(path)
    info = DbFileInfo(path=path, base=base, kind=kind, size=st.st_size, mtime_ns=st.st_mtime_ns)
    if os.path.exists(base + ".lock.db"):
        info.lock_file = base + ".lock.db"
    if kind == "mv":
        try:
            with open(path, "rb") as f:
                raw = f.read(4096)
            # The store header is written twice (block 0 and 1); both start with "H:2,"
            text = raw.split(b"\n", 1)[0].decode("latin-1")
            if not text.startswith("H:"):
                raise ValueError("kein MVStore-Header")
            hdr = {}
            for part in text.split(","):
                if ":" in part:
                    k, v = part.split(":", 1)
                    hdr[k.strip()] = v.strip()
            info.header = hdr
            info.fmt = int(hdr.get("format", "0"), 16) or None
        except Exception as e:  # noqa: BLE001
            info.header_error = str(e)
    return info


def unique_dir(parent: str, prefix: str) -> str:
    """Create a new, guaranteed unique work directory (timestamp + random suffix)."""
    import tempfile
    os.makedirs(parent, exist_ok=True)
    return tempfile.mkdtemp(prefix=f"{prefix}_{time.strftime('%Y%m%d_%H%M%S')}_", dir=os.path.abspath(parent))


def file_fingerprint(path: str) -> tuple[int, int]:
    st = os.stat(path)
    return st.st_size, st.st_mtime_ns


# ---------------------------------------------------------------------------
# JVM / driver handling
# ---------------------------------------------------------------------------

_jvm_lock = threading.Lock()


MIN_JAVA = 11   # H2 2.3+ needs Java 11, JPype >= 1.5 needs Java 9
JAVA_DOWNLOAD_HINT = ("Bitte zusätzlich Java 21 (LTS, 64 Bit) installieren, z.B. Eclipse Temurin von "
                      "https://adoptium.net – eine vorhandene Java-8-Installation kann bleiben, das Tool "
                      "findet die neue Version selbst.")
_started_jvm: dict = {}


@dataclass
class JavaInstall:
    jvm: str                 # path to jvm.dll / libjvm.so / libjvm.dylib
    home: str
    version: str             # e.g. "21.0.5" or "1.8.0_511"
    major: int               # 8, 11, 17, 21, ...

    def text(self) -> str:
        return f"Java {self.major} ({self.version}) – {self.home}"


def _java_major(version: str) -> int:
    m = re.match(r"(\d+)(?:\.(\d+))?", version or "")
    if not m:
        return 0
    a = int(m.group(1))
    return int(m.group(2) or 0) if a == 1 else a      # "1.8.0_511" -> 8


def _java_home_of(jvm: str) -> str:
    """First ancestor directory of the JVM library that contains bin/java(.exe)."""
    exe = "java.exe" if os.name == "nt" else "java"
    d = os.path.dirname(os.path.realpath(jvm))
    for _ in range(6):
        if os.path.isfile(os.path.join(d, "bin", exe)):
            return d
        nd = os.path.dirname(d)
        if nd == d:
            break
        d = nd
    return os.path.dirname(os.path.dirname(os.path.realpath(jvm)))


def java_version_of(jvm: str) -> Optional[str]:
    """Version of the Java installation that contains this JVM library."""
    import subprocess
    home = _java_home_of(jvm)
    for cand in (home, os.path.dirname(home)):
        rel = os.path.join(cand, "release")
        if os.path.isfile(rel):
            with open(rel, encoding="utf-8", errors="replace") as f:
                m = re.search(r'JAVA_VERSION="([^"]+)"', f.read())
            if m:
                return m.group(1)
    exe = "java.exe" if os.name == "nt" else "java"
    for cand in (os.path.join(home, "bin", exe), os.path.join(home, "jre", "bin", exe),
                 os.path.join(os.path.dirname(home), "bin", exe)):
        if os.path.isfile(cand):
            try:
                kw = {"creationflags": 0x08000000} if os.name == "nt" else {}   # CREATE_NO_WINDOW
                r = subprocess.run([cand, "-version"], capture_output=True, text=True, timeout=20, **kw)
                m = re.search(r'version "([^"]+)"', r.stderr + r.stdout)
                if m:
                    return m.group(1)
            except (OSError, subprocess.SubprocessError):
                pass
    return None


def _jvm_candidates() -> list[str]:
    import glob
    pats: list[str] = []
    homes = [os.environ.get("JAVA_HOME"), os.environ.get("JDK_HOME"), os.environ.get("JRE_HOME")]
    for h in homes:
        if h:
            pats += [os.path.join(h, "bin", "server", "jvm.dll"), os.path.join(h, "jre", "bin", "server", "jvm.dll"),
                     os.path.join(h, "lib", "server", "libjvm.so"), os.path.join(h, "jre", "lib", "*", "server", "libjvm.so"),
                     os.path.join(h, "lib", "server", "libjvm.dylib")]
    if os.name == "nt":
        roots = [os.environ.get(v) for v in ("ProgramFiles", "ProgramW6432", "LOCALAPPDATA")]
        for r in filter(None, roots):
            for vendor in ("Eclipse Adoptium", "Eclipse Foundation", "Java", "Microsoft", "Zulu", "Amazon Corretto",
                           "BellSoft", "OpenJDK", "AdoptOpenJDK", "Semeru", "Programs\\Eclipse Adoptium"):
                pats += [os.path.join(r, vendor, "*", "bin", "server", "jvm.dll"),
                         os.path.join(r, vendor, "*", "jre", "bin", "server", "jvm.dll")]
    else:
        pats += ["/usr/lib/jvm/*/lib/server/libjvm.so", "/usr/lib/jvm/*/jre/lib/*/server/libjvm.so",
                 "/usr/java/*/lib/server/libjvm.so", "/opt/*/lib/server/libjvm.so",
                 "/Library/Java/JavaVirtualMachines/*/Contents/Home/lib/server/libjvm.dylib",
                 os.path.expanduser("~/Library/Java/JavaVirtualMachines/*/Contents/Home/lib/server/libjvm.dylib")]
    out: list[str] = []
    for p in pats:
        for f in glob.glob(p):
            f = os.path.realpath(f)
            if f not in out:
                out.append(f)
    try:
        import jpype
        d = os.path.realpath(jpype.getDefaultJVMPath())
        if d not in out:
            out.append(d)
    except Exception:  # noqa: BLE001
        pass
    return out


def find_java_installs() -> list[JavaInstall]:
    res = []
    for jvm in _jvm_candidates():
        v = java_version_of(jvm)
        res.append(JavaInstall(jvm, _java_home_of(jvm), v or "unbekannt", _java_major(v or "")))
    res.sort(key=lambda j: j.major, reverse=True)
    return res


def choose_jvm(jvm_path: Optional[str] = None) -> JavaInstall:
    """Explicit path (checked) or the newest installed Java >= MIN_JAVA."""
    if jvm_path:
        p = jvm_path
        if os.path.isdir(p):      # a Java home directory was given
            found = [j for j in find_java_installs() if os.path.realpath(j.home).startswith(os.path.realpath(p))]
            if not found:
                raise RuntimeError(f"Im Verzeichnis {p} wurde keine jvm.dll/libjvm.so gefunden.")
            j = found[0]
        else:
            v = java_version_of(p) or "unbekannt"
            j = JavaInstall(os.path.realpath(p), _java_home_of(p), v, _java_major(v))
        if 0 < j.major < MIN_JAVA:
            raise RuntimeError(f"Die eingestellte {j.text()} ist zu alt; benötigt wird Java {MIN_JAVA} "
                               f"oder neuer.\n{JAVA_DOWNLOAD_HINT}")
        return j
    installs = find_java_installs()
    ok = [j for j in installs if j.major >= MIN_JAVA]
    if ok:
        return ok[0]
    unknown = [j for j in installs if j.major == 0]
    if unknown:
        return unknown[0]
    if installs:
        raise RuntimeError("Gefundene Java-Versionen sind zu alt (benötigt Java "
                           f"{MIN_JAVA} oder neuer):\n  " + "\n  ".join(j.text() for j in installs)
                           + f"\n\nHinweis: Nach Java 8 (\"1.8\") kamen Java 9, 10, 11 … 25.\n{JAVA_DOWNLOAD_HINT}")
    raise RuntimeError(f"Keine Java-Installation gefunden.\n{JAVA_DOWNLOAD_HINT}")


def start_jvm(jvm_path: Optional[str] = None, max_heap: Optional[str] = None) -> JavaInstall:
    import jpype
    with _jvm_lock:
        if jpype.isJVMStarted():
            return _started_jvm.get("java")
        j = choose_jvm(jvm_path)
        args = ["-Duser.timezone=UTC", "-Dfile.encoding=UTF-8"]
        if max_heap:
            args.append(f"-Xmx{max_heap}")
        try:
            jpype.startJVM(j.jvm, *args, convertStrings=True)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"Java konnte nicht gestartet werden: {j.text()}\n{e}\n"
                               "Python und Java müssen beide 64 Bit sein.") from e
        _started_jvm["java"] = j
        log.info("JVM: %s", j.text())
        return j


def java_error_text(e: BaseException) -> str:
    try:
        import jpype
        if isinstance(e, jpype.JException):
            msg = e.getMessage()
            return f"{e.getClass().getSimpleName()}: {msg}"
    except Exception:  # noqa: BLE001
        pass
    return f"{type(e).__name__}: {e}"


def java_error_code(e: BaseException) -> Optional[int]:
    try:
        return int(e.getErrorCode())  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return None


class H2Driver:
    """One H2 jar, loaded in an isolated class loader."""

    _cache: dict[str, "H2Driver"] = {}

    def __init__(self, jar: str):
        import jpype
        start_jvm()
        self.jar = os.path.abspath(jar)
        if not os.path.isfile(self.jar):
            raise FileNotFoundError(self.jar)
        URLClassLoader = jpype.JClass("java.net.URLClassLoader")
        File = jpype.JClass("java.io.File")
        ClassLoader = jpype.JClass("java.lang.ClassLoader")
        Class = jpype.JClass("java.lang.Class")
        try:
            parent = ClassLoader.getPlatformClassLoader()          # Java 9+
        except AttributeError:
            parent = ClassLoader.getSystemClassLoader().getParent()  # Java 8: extension loader
        self.loader = URLClassLoader([File(self.jar).toURI().toURL()], parent)
        self._Class = Class
        drv_cls = Class.forName("org.h2.Driver", True, self.loader)
        self.driver = drv_cls.getDeclaredConstructor().newInstance()
        c = self.connect("jdbc:h2:mem:", "sa", "")
        try:
            rs = c.createStatement().executeQuery("SELECT H2VERSION()")
            rs.next()
            self.version = str(rs.getString(1))
        finally:
            c.close()
        self.fmt = format_for_version(self.version)

    @classmethod
    def get(cls, jar: str) -> "H2Driver":
        key = os.path.abspath(jar)
        if key not in cls._cache:
            cls._cache[key] = H2Driver(key)
        return cls._cache[key]

    @property
    def v1(self) -> bool:
        return is_v1(self.version)

    def connect(self, url: str, user: str, password: str):
        import jpype
        props = jpype.JClass("java.util.Properties")()
        props.setProperty("user", user or "")
        props.setProperty("password", password or "")
        conn = self.driver.connect(url, props)
        if conn is None:
            raise RuntimeError(f"Treiber akzeptiert URL nicht: {url}")
        return conn

    def run_tool(self, class_name: str, args: list[str]) -> None:
        import jpype
        cls = self._Class.forName(class_name, True, self.loader)
        tool = cls.getDeclaredConstructor().newInstance()
        tool.runTool(jpype.JArray(jpype.JString)(args))


def query_all(conn, sql: str, params: Iterable = ()) -> list[tuple]:
    ps = conn.prepareStatement(sql)
    try:
        for i, p in enumerate(params, 1):
            ps.setObject(i, p)
        rs = ps.executeQuery()
        n = rs.getMetaData().getColumnCount()
        out = []
        while rs.next():
            out.append(tuple(rs.getObject(i) for i in range(1, n + 1)))
        return out
    finally:
        ps.close()


def execute(conn, sql: str) -> None:
    st = conn.createStatement()
    try:
        st.execute(sql)
    finally:
        st.close()


def make_ts(ms: int):
    import jpype
    return jpype.JClass("java.sql.Timestamp")(int(ms))


def ts_ms(obj) -> Optional[int]:
    if obj is None:
        return None
    if hasattr(obj, "getTime"):
        return int(obj.getTime())
    # java.time.LocalDateTime (interpreted as UTC)
    if hasattr(obj, "toEpochSecond"):
        import jpype
        utc = jpype.JClass("java.time.ZoneOffset").UTC
        return int(obj.toEpochSecond(utc)) * 1000 + int(obj.getNano()) // 1_000_000
    raise TypeError(f"Unbekannter Zeitstempeltyp: {type(obj)}")


def py(obj):
    """Convert simple java boxed values to python values (for display/compare)."""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    try:
        import jpype
        if isinstance(obj, jpype.JObject):
            cn = str(obj.getClass().getName())
            if cn in ("java.lang.Integer", "java.lang.Long", "java.lang.Short", "java.lang.Byte"):
                return int(obj.longValue())
            if cn in ("java.lang.Double", "java.lang.Float"):
                return float(obj.doubleValue())
            if cn == "java.lang.Boolean":
                return bool(obj.booleanValue())
            if hasattr(obj, "getTime"):
                return ms_to_str(int(obj.getTime()))
            return str(obj.toString())
    except Exception:  # noqa: BLE001
        pass
    return obj


# ---------------------------------------------------------------------------
# Table metadata
# ---------------------------------------------------------------------------

@dataclass
class TableInfo:
    name: str
    columns: list[tuple[str, str]]          # (name, type)
    is_data: bool = False
    min_ms: Optional[int] = None
    max_ms: Optional[int] = None
    count: Optional[int] = None
    error: Optional[str] = None
    datapoint: Optional[dict] = None       # row from DATA_POINTS

    @property
    def column_names(self) -> list[str]:
        return [c for c, _ in self.columns]

    @property
    def ts_col(self) -> Optional[str]:
        for c, _ in self.columns:
            if c.upper() == TS_COL:
                return c
        return None

    def label(self) -> str:
        dp = self.datapoint or {}
        return str(dp.get("DISPLAY_NAME") or "") or self.name


# progress(msg, frac=None, detail=False)
#   frac:   0..1 for the progress bar, None = unchanged/unknown
#   detail: True = frequent status update (status line only, not written to the log)
ProgressCb = Callable[..., None]


def _noop_progress(msg: str, frac: Optional[float] = None, detail: bool = False) -> None:
    pass


def fmt_n(n: int) -> str:
    """1234567 -> '1.234.567'"""
    return f"{int(n):,}".replace(",", ".")


def fmt_mb(b: float) -> str:
    return f"{b / 1e6:,.1f} MB".replace(",", "X").replace(".", ",").replace("X", ".")


def fmt_dur(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600}:{sec // 60 % 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60}:{sec % 60:02d}"


def _dir_size(path: str) -> int:
    tot = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                tot += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return tot


class Ticker:
    """Emits a status update every second while a blocking step runs.

    ``watch`` may be a file or directory whose size is reported (e.g. the
    database file H2 is writing), so the user sees that something happens.
    """

    def __init__(self, progress: ProgressCb, text: str, watch: Optional[str] = None, interval: float = 1.0):
        self.progress, self.text, self.watch, self.interval = progress, text, watch, interval
        self._stop = threading.Event()
        self._t0 = time.time()
        self._th = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self._stop.wait(self.interval):
            extra = ""
            if self.watch:
                try:
                    size = _dir_size(self.watch) if os.path.isdir(self.watch) else os.path.getsize(self.watch)
                    extra = f", {fmt_mb(size)} geschrieben"
                except OSError:
                    pass
            self.progress(f"{self.text} – läuft seit {fmt_dur(time.time() - self._t0)}{extra}", None, True)

    def __enter__(self):
        self._th.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._th.join(2)
        return False


def copy_with_progress(src: str, dst: str, progress: ProgressCb, text: str,
                       cancel: Optional[threading.Event] = None) -> None:
    total = os.path.getsize(src)
    done = 0
    last = 0.0
    t0 = time.time()
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        while True:
            _check_cancel(cancel)
            b = fi.read(8 * 1024 * 1024)
            if not b:
                break
            fo.write(b)
            done += len(b)
            now = time.time()
            if now - last >= 0.5:
                last = now
                rate = done / max(0.001, now - t0)
                progress(f"{text}: {fmt_mb(done)} von {fmt_mb(total)} ({rate / 1e6:.0f} MB/s)",
                         None, True)
    shutil.copystat(src, dst)


def _check_cancel(cancel: Optional[threading.Event]) -> None:
    if cancel is not None and cancel.is_set():
        raise Cancelled()


# ---------------------------------------------------------------------------
# Opened database (always a working copy)
# ---------------------------------------------------------------------------

class Database:
    def __init__(self, label: str, file_path: str, jar: str, user: str = "sa",
                 password: str = "", workdir: str = ".", copy: bool = True,
                 url_options: str = ""):
        self.label = label
        self.info = inspect_db_file(file_path)
        self.jar = jar
        self.user = user
        self.password = password
        self.workdir = os.path.abspath(workdir)
        self.copy = copy
        self.url_options = url_options
        self.driver: Optional[H2Driver] = None
        self.conn = None
        self.copy_base: Optional[str] = None
        self.origin = self.info.path          # file the user selected (may be a zip)
        self.original_fingerprint = file_fingerprint(self.info.path)
        self.tables: dict[str, TableInfo] = {}
        self.datapoints: list[dict] = []
        self.recovered_from: Optional[str] = None
        self.analysis = None

    # -- lifecycle ---------------------------------------------------------
    def _url(self) -> str:
        # ACCESS_MODE_DATA=r: H2 must never write to (and e.g. truncate) the copy;
        # older 1.4 versions do this when they roll back to an old chunk.
        opts = ";IFEXISTS=TRUE;ACCESS_MODE_DATA=r"
        if self.info.kind == "page":
            opts += ";MV_STORE=FALSE"
        if self.driver and not self.driver.v1:
            opts += ";LAZY_QUERY_EXECUTION=TRUE"
        elif self.driver and _version_tuple(self.driver.version) >= (1, 4, 198):
            opts += ";LAZY_QUERY_EXECUTION=1"
        return f"jdbc:h2:file:{self.copy_base}{opts}{self.url_options}"

    def open(self, progress: ProgressCb = _noop_progress) -> None:
        self.driver = H2Driver.get(self.jar)
        if self.info.kind == "mv" and self.info.fmt and self.driver.fmt and self.info.fmt != self.driver.fmt:
            raise RuntimeError(
                f"Datenbankformat ({self.info.format_text}) passt nicht zur H2-Version "
                f"{self.driver.version} des gewählten JARs. Bitte ein passendes JAR wählen.")
        if self.info.kind == "page" and not self.driver.v1:
            raise RuntimeError("PageStore-Dateien (.h2.db) können nur mit H2 1.x geöffnet werden.")
        ext = ".mv.db" if self.info.kind == "mv" else ".h2.db"
        if self.copy:
            dest_dir = os.path.join(self.workdir, f"src_{self.label}")
            os.makedirs(dest_dir, exist_ok=True)
            name = os.path.basename(self.info.base)
            self.copy_base = os.path.join(dest_dir, name)
            dest = self.copy_base + ext
            if os.path.exists(dest):
                os.remove(dest)
            progress(f"{self.label}: kopiere {self.info.path} ({fmt_mb(self.info.size)}) ins Arbeitsverzeichnis",
                     None)
            copy_with_progress(self.info.path, dest, progress, f"{self.label}: kopiere")
            if self.info.kind == "mv":
                # verify that the copy is identical in size
                if os.path.getsize(dest) != self.info.size:
                    raise RuntimeError("Kopie unvollständig (Datei wurde während des Kopierens verändert?)")
        else:
            self.copy_base = self.info.base
        if self.info.kind == "mv":
            try:
                from .carve import analyze_mvstore
                self.analysis = analyze_mvstore(self.copy_base + ext)
                if self.analysis.rollback_suspected:
                    progress(f"{self.label}: {self.analysis.text()}", None)
            except Exception as e:  # noqa: BLE001
                log.info("analysis failed: %s", e)
        progress(f"{self.label}: öffne Kopie mit H2 {self.driver.version} ...", None)
        self.conn = self.driver.connect(self._url(), self.user, self.password)
        self.conn.setAutoCommit(True)

    def reconnect(self) -> None:
        try:
            if self.conn is not None and not self.conn.isClosed():
                self.conn.createStatement().executeQuery("SELECT 1").close()
                return
        except Exception:  # noqa: BLE001
            pass
        try:
            if self.conn is not None:
                self.conn.close()
        except Exception:  # noqa: BLE001
            pass
        self.conn = self.driver.connect(self._url(), self.user, self.password)

    def close(self) -> None:
        try:
            if self.conn is not None and not self.conn.isClosed():
                self.conn.close()
        except Exception:  # noqa: BLE001
            pass
        self.conn = None

    def set_origin(self, path: str) -> None:
        self.origin = os.path.abspath(path)
        self.original_fingerprint = file_fingerprint(self.origin)

    def original_unchanged(self) -> bool:
        return file_fingerprint(self.origin) == self.original_fingerprint

    # -- metadata ----------------------------------------------------------
    def load_tables(self) -> dict[str, TableInfo]:
        rows = query_all(self.conn,
                         "SELECT TABLE_NAME, TABLE_TYPE FROM INFORMATION_SCHEMA.TABLES "
                         "WHERE TABLE_SCHEMA = 'PUBLIC'")
        names = sorted(str(r[0]) for r in rows if str(r[1]).upper() in ("TABLE", "BASE TABLE"))
        md = self.conn.getMetaData()
        tables: dict[str, TableInfo] = {}
        for n in names:
            cols = []
            rs = md.getColumns(None, "PUBLIC", n, None)
            while rs.next():
                cols.append((str(rs.getString("COLUMN_NAME")), str(rs.getString("TYPE_NAME")),
                             int(rs.getInt("ORDINAL_POSITION"))))
            rs.close()
            cols.sort(key=lambda c: c[2])
            ti = TableInfo(name=n, columns=[(c[0], c[1]) for c in cols])
            upper = {c.upper() for c in ti.column_names}
            ti.is_data = n.upper() != DATA_POINTS and TS_COL in upper and "VALUE" in upper
            tables[n] = ti
        self.tables = tables
        return tables

    def load_datapoints(self) -> list[dict]:
        self.datapoints = []
        if DATA_POINTS not in self.tables:
            return []
        cols = self.tables[DATA_POINTS].column_names
        try:
            rows = query_all(self.conn, f"SELECT {', '.join(q(c) for c in cols)} FROM {q(DATA_POINTS)}")
        except Exception as e:  # noqa: BLE001
            self.tables[DATA_POINTS].error = java_error_text(e)
            return []
        for r in rows:
            dp = {c.upper(): py(v) for c, v in zip(cols, r)}
            dp["_RAW"] = {c.upper(): v for c, v in zip(cols, r)}
            self.datapoints.append(dp)
        for dp in self.datapoints:
            name = dp.get("TABLE_NAME") or historian_table_name(
                dp.get("INTERFACE", ""), dp.get("ADDRESS", ""), dp.get("IDENTIFIER", ""))
            dp["_TABLE"] = name if name in self.tables else None
            if dp["_TABLE"]:
                self.tables[name].datapoint = dp
        return self.datapoints

    def scan(self, progress: ProgressCb = _noop_progress, cancel: Optional[threading.Event] = None) -> None:
        """Determine min/max timestamp and row count of every data table."""
        self.load_tables()
        self.load_datapoints()
        data = [t for t in self.tables.values() if t.is_data]
        for i, t in enumerate(data):
            _check_cancel(cancel)
            if i % 20 == 0:
                progress(f"{self.label}: analysiere Datenpunkt {i + 1}/{len(data)} ({t.name})",
                         i / max(1, len(data)), True)
            self.table_stats(t)

    def table_stats(self, t: TableInfo) -> None:
        tc = q(t.ts_col)
        try:
            rows = query_all(self.conn, f"SELECT MIN({tc}), MAX({tc}), COUNT(*) FROM {q(t.name)}")
            mn, mx, cnt = rows[0]
            t.min_ms, t.max_ms, t.count = ts_ms(mn), ts_ms(mx), int(py(cnt))
            t.error = None
        except Exception as e:  # noqa: BLE001
            t.error = java_error_text(e)
            self.reconnect()
            # try at least min/max separately (uses index, avoids full scan)
            try:
                mn = query_all(self.conn, f"SELECT {tc} FROM {q(t.name)} ORDER BY {tc} LIMIT 1")
                mx = query_all(self.conn, f"SELECT {tc} FROM {q(t.name)} ORDER BY {tc} DESC LIMIT 1")
                t.min_ms = ts_ms(mn[0][0]) if mn else None
                t.max_ms = ts_ms(mx[0][0]) if mx else None
            except Exception:  # noqa: BLE001
                self.reconnect()

    def overall_range(self) -> tuple[Optional[int], Optional[int], int]:
        mins = [t.min_ms for t in self.tables.values() if t.is_data and t.min_ms is not None]
        maxs = [t.max_ms for t in self.tables.values() if t.is_data and t.max_ms is not None]
        total = sum(t.count or 0 for t in self.tables.values() if t.is_data)
        return (min(mins) if mins else None, max(maxs) if maxs else None, total)

    def daily_counts(self, table: str, cancel: Optional[threading.Event] = None) -> list[tuple[int, int]]:
        """[(day_start_ms, rows)] for one data table (for the coverage view).

        Days that cannot be read are returned with rows = -1.
        """
        t = self.tables[table]
        tc = q(t.ts_col)
        try:
            # COUNT(<non-ts column>) forces reading the data rows, not only the TS index
            rows = query_all(self.conn,
                             f"SELECT CAST({tc} AS DATE) D, COUNT(*), COUNT({self._probe_col(t)}) "
                             f"FROM {q(table)} GROUP BY D ORDER BY D")
        except Exception:  # noqa: BLE001
            self.reconnect()
            return self._daily_counts_slow(t, cancel)
        out = []
        for d, n, _ in rows:
            if d is None:
                continue
            ms = ts_ms(d) if hasattr(d, "getTime") else None
            if ms is None:  # java.time.LocalDate
                ms = int(d.toEpochDay()) * DAY_MS
            out.append((ms, int(py(n))))
        return out

    @staticmethod
    def _probe_col(t: TableInfo) -> str:
        others = [c for c in t.column_names if c != t.ts_col]
        return q(others[0] if others else t.ts_col)

    def _daily_counts_slow(self, t: TableInfo, cancel) -> list[tuple[int, int]]:
        """Fallback for defective tables: count day by day, mark unreadable days."""
        if t.min_ms is None or t.max_ms is None:
            raise RuntimeError("Zeitbereich der Tabelle unbekannt")
        tc = q(t.ts_col)
        sql = (f"SELECT COUNT(*), COUNT({self._probe_col(t)}) FROM {q(t.name)} "
               f"WHERE {tc} >= ? AND {tc} < ?")
        ps = self.conn.prepareStatement(sql)
        out = []
        day = t.min_ms - t.min_ms % DAY_MS
        while day <= t.max_ms:
            _check_cancel(cancel)
            try:
                ps.setTimestamp(1, make_ts(day))
                ps.setTimestamp(2, make_ts(day + DAY_MS))
                rs = ps.executeQuery()
                rs.next()
                n = int(rs.getLong(1))
                rs.close()
                if n:
                    out.append((day, n))
            except Exception:  # noqa: BLE001
                out.append((day, -1))
                self.reconnect()
                ps = self.conn.prepareStatement(sql)
            day += DAY_MS
        ps.close()
        return out

    def deep_check(self, progress: ProgressCb = _noop_progress, cancel: Optional[threading.Event] = None,
                   min_skip_ms: int = 600_000) -> dict[str, tuple[int, list[tuple[int, int]]]]:
        """Read every row of every data table. Returns {table: (rows_read, lost_intervals)}.

        Tables with unreadable parts get TableInfo.error set.
        """
        res = {}
        data = [t for t in self.tables.values() if t.is_data]
        for i, t in enumerate(data):
            _check_cancel(cancel)
            progress(f"{self.label}: prüfe Datenpunkt {i + 1}/{len(data)} ({t.name}), "
                     f"bisher {fmt_n(sum(v[0] for v in res.values()))} Werte gelesen",
                     i / max(1, len(data)), True)
            if i and i % max(1, len(data) // 10) == 0:
                progress(f"{self.label}: Prüfung {i * 100 // len(data)} % ({i}/{len(data)} Datenpunkte)", None)
            lo = t.min_ms if t.min_ms is not None else 0
            hi = t.max_ms if t.max_ms is not None else int(time.time() * 1000) + 365 * DAY_MS
            cols = t.column_names
            r = RobustReader(self, t.name, cols, cols.index(t.ts_col), 200_000, min_skip_ms, cancel)
            n = 0
            for b in r.batches(lo, hi, t.count):
                n += len(b)
            res[t.name] = (n, r.lost)
            if r.lost:
                progress(f"{self.label}: WARNUNG {t.name}: {len(r.lost)} unlesbare Bereiche", None)
            if r.lost or (t.count is not None and n != t.count):
                t.error = (f"{len(r.lost)} unlesbare Bereiche, {n} von {t.count} Werten lesbar: "
                           + "; ".join(f"{ms_to_str(a)}..{ms_to_str(b)}" for a, b in r.lost[:5]))
        return res

    def count_range(self, table: str, lo: Optional[int], hi: Optional[int]) -> int:
        t = self.tables[table]
        tc = q(t.ts_col)
        where, params = _range_where(tc, lo, hi)
        rows = query_all(self.conn, f"SELECT COUNT(*) FROM {q(table)}{where}", params)
        return int(py(rows[0][0]))

    def script_nodata(self, tables: Optional[list[str]] = None) -> list[str]:
        sql = "SCRIPT NODATA"
        if tables:
            sql += " TABLE " + ", ".join(q(t) for t in tables)
        return [str(r[0]) for r in query_all(self.conn, sql)]


def _range_where(tc: str, lo: Optional[int], hi: Optional[int]) -> tuple[str, list]:
    parts, params = [], []
    if lo is not None:
        parts.append(f"{tc} >= ?")
        params.append(make_ts(lo))
    if hi is not None:
        parts.append(f"{tc} <= ?")
        params.append(make_ts(hi))
    return (" WHERE " + " AND ".join(parts)) if parts else "", params


# ---------------------------------------------------------------------------
# ZIP backups (SQL script or database file inside)
# ---------------------------------------------------------------------------

@dataclass
class ZipSourceInfo:
    path: str
    kind: str                      # "script" | "db"
    entry: str
    size: int                      # uncompressed size of the entry
    script_version: Optional[str] = None   # "2.5.252", or None = H2 1.x (no version line)

    def text(self) -> str:
        if self.kind == "db":
            return f"ZIP mit Datenbankdatei {self.entry} ({self.size / 1e6:.1f} MB)"
        v = self.script_version or "1.x (keine Versionszeile)"
        return f"ZIP mit SQL-Skript {self.entry} ({self.size / 1e6:.1f} MB), erzeugt von H2 {v}"


_DATETIME_COL = re.compile(rb'^(    "(?:[^"]|"")+" )DATETIME\b')
_DATETIME_COL_STR = re.compile(r'^(\s+"(?:[^"]|"")+" )DATETIME\b', re.M)


def convert_1x_ddl(stmt: str) -> str:
    """Column types of H2 1.x scripts that H2 2.x (FROM_1X) does not accept any more."""
    return _DATETIME_COL_STR.sub(r"\1TIMESTAMP", stmt) if "DATETIME" in stmt else stmt


def inspect_zip(path: str) -> ZipSourceInfo:
    with zipfile.ZipFile(path) as z:
        infos = [i for i in z.infolist() if not i.is_dir()]
        dbs = [i for i in infos if i.filename.endswith((".mv.db", ".h2.db"))]
        if dbs:
            i = max(dbs, key=lambda x: x.file_size)
            return ZipSourceInfo(os.path.abspath(path), "db", i.filename, i.file_size)
        cands = [i for i in infos if i.filename.lower().endswith(".sql")] or infos
        if not cands:
            raise ValueError("ZIP-Datei ist leer")
        i = max(cands, key=lambda x: x.file_size)
        with z.open(i) as f:
            head = f.read(4096).decode("utf-8", "replace")
        if "CREATE" not in head.upper() and not head.lstrip().startswith("--"):
            raise ValueError(f"{i.filename} sieht nicht wie ein H2-SQL-Skript aus")
        m = re.match(r"\s*--\s*H2\s+(\d+\.\d+\.\d+)", head)
        return ZipSourceInfo(os.path.abspath(path), "script", i.filename, i.file_size,
                             m.group(1) if m else None)


def import_zip_source(path: str, jar: str, workdir: str, user: str = "sa", password: str = "",
                      progress: ProgressCb = _noop_progress,
                      cancel: Optional[threading.Event] = None) -> tuple[str, ZipSourceInfo]:
    """Extract a zip backup into the work directory and return a .mv.db/.h2.db to open.

    A SQL script is imported with RUNSCRIPT into a new database (FROM_1X when a
    1.x script is imported with an H2 2.x jar). The zip itself is only read.
    """
    zi = inspect_zip(path)
    drv = H2Driver.get(jar)
    zdir = unique_dir(workdir, "zip")
    target = os.path.join(zdir, os.path.basename(zi.entry))
    progress(f"ZIP: entpacke {zi.entry} ({fmt_mb(zi.size)})", None)
    convert = zi.kind == "script" and zi.script_version is None and not drv.v1
    with zipfile.ZipFile(path) as z, z.open(zi.entry) as fi, open(target, "wb") as fo:
        done = 0
        last = 0.0
        if convert:
            # H2 1.x -> 2.x: column types that FROM_1X does not translate
            n = 0
            for line in fi:
                if line.startswith(b"    \"") and b"DATETIME" in line:
                    line = _DATETIME_COL.sub(rb"\1TIMESTAMP", line)
                fo.write(line)
                done += len(line)
                n += 1
                if n % 100_000 == 0:
                    _check_cancel(cancel)
                    if time.time() - last >= 0.5:
                        last = time.time()
                        progress(f"ZIP: entpacke {fmt_mb(done)} von {fmt_mb(zi.size)}",
                                 done / max(1, zi.size), True)
        else:
            while True:
                _check_cancel(cancel)
                b = fi.read(16 * 1024 * 1024)
                if not b:
                    break
                fo.write(b)
                done += len(b)
                progress(f"ZIP: entpacke {fmt_mb(done)} von {fmt_mb(zi.size)}", done / max(1, zi.size), True)
    if zi.kind == "db":
        return target, zi
    sv = zi.script_version
    if sv is not None and drv.v1:
        raise RuntimeError(f"Das Skript stammt von H2 {sv}; bitte ein H2-2.x-JAR (>= {sv}) wählen.")
    if sv is not None and _version_tuple(drv.version) < _version_tuple(sv):
        raise RuntimeError(f"Das Skript stammt von H2 {sv}, gewählt ist H2 {drv.version}. "
                           f"Bitte ein JAR mit Version >= {sv} wählen (z.B. über 'H2 laden…').")
    base = os.path.join(zdir, re.sub(r"\.zip$", "", os.path.basename(path), flags=re.I) + "_import")
    sql = f"RUNSCRIPT FROM '{target.replace(chr(39), chr(39) * 2)}' CHARSET 'UTF-8'"
    if sv is None and not drv.v1:
        sql += " FROM_1X"
    progress(f"ZIP: importiere Skript mit H2 {drv.version}{' (FROM_1X)' if 'FROM_1X' in sql else ''} "
             f"– dauert bei großen Sicherungen länger ...", None)
    conn = drv.connect(f"jdbc:h2:file:{base}", user, password)
    st = conn.createStatement()
    done = threading.Event()

    def watch():
        while not done.wait(0.5):
            if cancel is not None and cancel.is_set():
                try:
                    st.cancel()
                except Exception:  # noqa: BLE001
                    pass
                return
    threading.Thread(target=watch, daemon=True).start()
    t0 = time.time()
    try:
        with Ticker(progress, f"ZIP: spiele Skript ({fmt_mb(zi.size)}) ein", base + ".mv.db"):
            st.execute(sql)
    except Exception:
        _check_cancel(cancel)
        raise
    finally:
        done.set()
        st.close()
        conn.close()
    os.remove(target)
    progress(f"ZIP: Import fertig nach {fmt_dur(time.time() - t0)} "
             f"(Datenbank {fmt_mb(os.path.getsize(base + '.mv.db'))})", None)
    return base + ".mv.db", zi


def open_source(label: str, path: str, jar: str, user: str = "sa", password: str = "",
                workdir: str = ".", progress: ProgressCb = _noop_progress,
                cancel: Optional[threading.Event] = None) -> "Database":
    """Open a source given as .mv.db/.h2.db or as .zip backup (always via a copy)."""
    if path.lower().endswith(".zip"):
        dbpath, zi = import_zip_source(path, jar, workdir, user, password, progress, cancel)
        db = Database(label, dbpath, jar, user, password, workdir, copy=False)
        db.set_origin(path)
        db.recovered_from = os.path.abspath(path)
        db.zip_info = zi
    else:
        db = Database(label, path, jar, user, password, workdir)
    db.open(progress)
    return db


# ---------------------------------------------------------------------------
# Recovery of databases that cannot be opened
# ---------------------------------------------------------------------------

def recover_database(file_path: str, jar: str, workdir: str, user: str = "sa", password: str = "",
                     progress: ProgressCb = _noop_progress) -> str:
    """Run org.h2.tools.Recover on a copy and import the script into a new DB.

    Returns the path of the recovered .mv.db (inside workdir). The original
    file is not touched.
    """
    info = inspect_db_file(file_path)
    drv = H2Driver.get(jar)
    rdir = unique_dir(workdir, "recover")
    name = os.path.basename(info.base)
    ext = ".mv.db" if info.kind == "mv" else ".h2.db"
    progress(f"Recover: kopiere {file_path} ...", None)
    shutil.copyfile(info.path, os.path.join(rdir, name + ext))
    progress(f"Recover: H2 {drv.version} org.h2.tools.Recover läuft (kann dauern) ...", None)
    with Ticker(progress, "Recover: lese defekte Datenbank", rdir):
        drv.run_tool("org.h2.tools.Recover", ["-dir", rdir, "-db", name])
    script = os.path.join(rdir, name + ".h2.sql")
    if not os.path.exists(script):
        raise RuntimeError("Recover hat kein SQL-Skript erzeugt.")
    progress(f"Recover: Skript {os.path.getsize(script) / 1e6:.1f} MB erzeugt, importiere ...", None)
    newbase = os.path.join(rdir, name + "_recovered")
    conn = drv.connect(f"jdbc:h2:file:{newbase}", user, password)
    try:
        with Ticker(progress, "Recover: spiele gerettetes Skript ein", newbase + ".mv.db"):
            execute(conn, f"RUNSCRIPT FROM '{script.replace(chr(39), chr(39) * 2)}'")
    finally:
        conn.close()
    try:
        os.remove(os.path.join(rdir, name + ext))  # remove the defective copy
    except OSError:
        pass
    progress("Recover: fertig", None)
    return newbase + ".mv.db"


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------

@dataclass
class SourceSpec:
    db: Database
    tables: Optional[set[str]] = None     # selected data tables, None = all
    t_from: Optional[int] = None          # inclusive, ms
    t_to: Optional[int] = None            # inclusive, ms

    def selected(self, table: str) -> bool:
        t = self.db.tables.get(table)
        if t is None or not t.is_data:
            return False
        return self.tables is None or table in self.tables


@dataclass
class MergeOptions:
    output_path: str                      # desired .mv.db path of the new database
    target_jar: str
    template: int = 0                     # index into sources: schema + other tables
    dedup: str = "timestamp"              # "timestamp" | "range"
    min_skip_ms: int = 3_600_000          # smallest interval given up when unreadable
    rows_per_query: int = 200_000
    batch_size: int = 5_000
    compact: bool = True
    overwrite: bool = False
    keep_workdir: bool = False
    workdir: Optional[str] = None
    user: Optional[str] = None            # credentials of the new DB (default: template's)
    password: Optional[str] = None


@dataclass
class TransferStat:
    table: str
    source: str
    read: int = 0
    inserted: int = 0
    dup: int = 0
    range_skip: int = 0
    failed: int = 0
    lost: list[tuple[int, int]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    min_ms: Optional[int] = None
    max_ms: Optional[int] = None


@dataclass
class MergeReport:
    output_path: str = ""
    stats: list[TransferStat] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    verify: dict = field(default_factory=dict)
    sources_unchanged: dict = field(default_factory=dict)
    duration_s: float = 0.0
    ok: bool = False

    def summary(self, max_items: int = 10) -> str:
        """Short version for the GUI log; the full table is in the report file."""
        tot = [sum(getattr(s, a) for s in self.stats) for a in ("read", "inserted", "dup", "range_skip", "failed")]
        lost = [s for s in self.stats if s.lost]
        errs = [s for s in self.stats if s.errors]
        bad = {k: v for k, v in self.verify.items() if v[0] != v[1]}
        L = [f"Ergebnis: {self.output_path}",
             f"Status: {'OK' if self.ok else 'FEHLER/WARNUNGEN'}, Dauer {fmt_dur(self.duration_s)}",
             f"Werte: {fmt_n(tot[0])} gelesen, {fmt_n(tot[1])} geschrieben, {fmt_n(tot[2])} Duplikate, "
             f"{fmt_n(tot[3])} wegen Zeitraum-Priorität übersprungen, {fmt_n(tot[4])} nicht schreibbar",
             f"Quelldateien unverändert: " + ", ".join(f"{k}={'ja' if v else 'NEIN'}"
                                                     for k, v in self.sources_unchanged.items()),
             f"Verifikation: {len(self.verify) - len(bad)} von {len(self.verify)} Tabellen stimmen"]
        if lost:
            L.append(f"WARNUNG: {len(lost)} Datenpunkte mit unlesbaren Bereichen, z.B. "
                     + ", ".join(f"{s.table} ({s.source})" for s in lost[:3]))
        if errs:
            L.append(f"FEHLER bei {len(errs)} Datenpunkten, z.B.:")
            L += [f"  {s.table} ({s.source}): {s.errors[0].splitlines()[0][:150]}" for s in errs[:max_items]]
        if bad:
            L.append(f"FEHLER: {len(bad)} Tabellen weichen bei der Verifikation ab")
        if self.warnings:
            L.append(f"{len(self.warnings)} Hinweise (z.B. neu vergebene DP_IDs) – siehe Bericht")
        return "\n".join(L)

    def text(self) -> str:
        L = [f"Ergebnis: {self.output_path}", f"Dauer: {self.duration_s:.1f} s",
             f"Status: {'OK' if self.ok else 'FEHLER/WARNUNGEN'}", ""]
        L.append("Quelldateien unverändert: " + ", ".join(
            f"{k}={'ja' if v else 'NEIN'}" for k, v in self.sources_unchanged.items()))
        if self.warnings:
            L.append("")
            L.append("Warnungen:")
            L += [f"  - {w}" for w in self.warnings]
        L.append("")
        L.append(f"{'Tabelle':50s} {'Quelle':6s} {'gelesen':>10s} {'eingefügt':>10s} "
                 f"{'Duplikat':>9s} {'Bereich':>8s} {'Fehler':>7s}  Zeitraum")
        tot = [0, 0, 0, 0, 0]
        for s in self.stats:
            L.append(f"{s.table[:50]:50s} {s.source[:6]:6s} {s.read:10d} {s.inserted:10d} "
                     f"{s.dup:9d} {s.range_skip:8d} {s.failed:7d}  "
                     f"{ms_to_str(s.min_ms)} .. {ms_to_str(s.max_ms)}")
            for a, b in s.lost:
                L.append(f"{'':50s}        NICHT LESBAR: {ms_to_str(a)} .. {ms_to_str(b)}")
            for e in s.errors[:5]:
                L.append(f"{'':50s}        Fehler: {e}")
            tot = [tot[0] + s.read, tot[1] + s.inserted, tot[2] + s.dup, tot[3] + s.range_skip, tot[4] + s.failed]
        L.append(f"{'SUMME':50s} {'':6s} {tot[0]:10d} {tot[1]:10d} {tot[2]:9d} {tot[3]:8d} {tot[4]:7d}")
        if self.verify:
            L.append("")
            bad = {k: v for k, v in self.verify.items() if v[0] != v[1]}
            L.append(f"Verifikation (Zeilen in neuer DB vs. erwartet): "
                     f"{len(self.verify) - len(bad)} von {len(self.verify)} Tabellen stimmen")
            for k, (exp, got) in bad.items():
                L.append(f"  ABWEICHUNG {k}: erwartet {exp}, vorhanden {got}")
        return "\n".join(L)


_NAME_RE = re.compile(r'((?:INDEX|CONSTRAINT|PRIMARY KEY)\s+)"PUBLIC"\."([^"]+)"')


class MergeJob:
    """Builds a new database from one or more (copied) source databases."""

    def __init__(self, sources: list[SourceSpec], opts: MergeOptions,
                 progress: ProgressCb = _noop_progress, cancel: Optional[threading.Event] = None):
        if not sources:
            raise ValueError("Keine Quelle angegeben")
        self.sources = sources
        self.opts = opts
        self.progress = progress
        self.cancel = cancel
        self.report = MergeReport()
        self.tconn = None
        self.tdrv: Optional[H2Driver] = None
        self.tbase: Optional[str] = None
        self._name_counter = 0
        self._temp_indexes: list[str] = []
        self._expected: dict[str, int] = {}
        self._inserted_total = 0
        self._t_data = time.time()

    # -- checks ------------------------------------------------------------
    def _check_output(self) -> str:
        out = os.path.abspath(self.opts.output_path)
        if not out.endswith(".mv.db"):
            out += ".mv.db"
        for s in self.sources:
            if os.path.normcase(out) == os.path.normcase(s.db.info.path):
                raise ValueError("Ausgabedatei darf keine Quelldatei sein (nicht-invasiver Betrieb).")
            if os.path.normcase(out) == os.path.normcase(getattr(s.db, "origin", s.db.info.path)):
                raise ValueError("Ausgabedatei darf keine Quelldatei sein (nicht-invasiver Betrieb).")
            rec = s.db.recovered_from
            if rec and os.path.normcase(out) == os.path.normcase(os.path.abspath(rec)):
                raise ValueError("Ausgabedatei darf keine Quelldatei sein (nicht-invasiver Betrieb).")
        if os.path.exists(out) and not self.opts.overwrite:
            raise FileExistsError(f"Ausgabedatei existiert bereits: {out}")
        return out

    # -- main --------------------------------------------------------------
    def run(self) -> MergeReport:
        t0 = time.time()
        out = self._check_output()
        self.report.output_path = out
        tmpl = self.sources[self.opts.template].db
        user = self.opts.user if self.opts.user is not None else tmpl.user
        pw = self.opts.password if self.opts.password is not None else tmpl.password
        workdir = self.opts.workdir or os.path.join(os.path.dirname(out), "historian_repair_work")
        tdir = unique_dir(workdir, "target")
        self.tbase = os.path.join(tdir, os.path.basename(out)[:-6])
        self.tdrv = H2Driver.get(self.opts.target_jar)
        for s in self.sources:
            if s.db.driver and not s.db.driver.v1 and self.tdrv.v1:
                raise ValueError("Ziel-H2-Version 1.x kann keine Daten aus einer H2-2.x-Quelle "
                                 "mit deren Schema aufnehmen. Bitte ein H2-2.x-JAR für das Ziel wählen.")
        self.progress(f"Erzeuge neue Datenbank mit H2 {self.tdrv.version}: {self.tbase}.mv.db", 0.0)
        self.tconn = self.tdrv.connect(f"jdbc:h2:file:{self.tbase}", user, pw)
        try:
            self._create_schema(tmpl)
            self._copy_other_tables()
            self._merge_datapoints()
            self._merge_data()
            self._drop_temp_indexes()
            self.tconn.commit()
            self.progress("Schließe neue Datenbank" + (" (Kompaktierung)" if self.opts.compact else ""), 0.97)
            self.tconn.setAutoCommit(True)
            if self.opts.compact:
                with Ticker(self.progress, "Kompaktiere neue Datenbank", os.path.dirname(self.tbase)):
                    execute(self.tconn, "SHUTDOWN COMPACT")
            self.tconn.close()
            self.tconn = None
            self._verify(user, pw)
            os.makedirs(os.path.dirname(out), exist_ok=True)
            if os.path.exists(out):
                os.remove(out)
            shutil.move(self.tbase + ".mv.db", out)
        finally:
            if self.tconn is not None:
                try:
                    self.tconn.rollback()
                    self.tconn.close()
                except Exception:  # noqa: BLE001
                    pass
            for s in self.sources:
                self.report.sources_unchanged[s.db.label] = s.db.original_unchanged()
            if not self.opts.keep_workdir:
                shutil.rmtree(tdir, ignore_errors=True)
        self.report.duration_s = time.time() - t0
        self.report.ok = (all(self.report.sources_unchanged.values())
                          and all(a == b for a, b in self.report.verify.values())
                          and not any(s.failed or s.lost for s in self.report.stats))
        self.progress("Fertig", 1.0)
        return self.report

    # -- schema ------------------------------------------------------------
    def _target_tables(self) -> set[str]:
        rows = query_all(self.tconn, "SELECT TABLE_NAME, TABLE_TYPE FROM INFORMATION_SCHEMA.TABLES "
                                     "WHERE TABLE_SCHEMA = 'PUBLIC'")
        return {str(r[0]) for r in rows if str(r[1]).upper() in ("TABLE", "BASE TABLE")}

    def _target_columns(self, table: str) -> list[str]:
        md = self.tconn.getMetaData()
        rs = md.getColumns(None, "PUBLIC", table, None)
        cols = []
        while rs.next():
            cols.append((int(rs.getInt("ORDINAL_POSITION")), str(rs.getString("COLUMN_NAME"))))
        rs.close()
        return [c for _, c in sorted(cols)]

    def _runscript(self, src: Database, lines: list[str], tag: str) -> None:
        path = f"{self.tbase}_{tag}.sql"
        convert = src.driver.v1 and not self.tdrv.v1
        with open(path, "w", encoding="utf-8") as f:
            for ln in lines:
                if convert:
                    ln = convert_1x_ddl(ln)
                f.write(ln.rstrip().rstrip(";") + ";\n")
        sql = f"RUNSCRIPT FROM '{path.replace(chr(39), chr(39) * 2)}' CHARSET 'UTF-8'"
        if src.driver.v1 and not self.tdrv.v1:
            sql += " FROM_1X"
        execute(self.tconn, sql)

    def _create_schema(self, tmpl: Database) -> None:
        self.progress(f"Übernehme Schema aus Vorlage {tmpl.label}", 0.01)
        lines = [ln for ln in tmpl.script_nodata() if not ln.lstrip().startswith("--")]
        self._runscript(tmpl, lines, "schema")
        self.tconn.setAutoCommit(False)

    def _ensure_table(self, src: Database, table: str) -> None:
        if table in self._target_tables():
            return
        stmts = []
        for ln in src.script_nodata([table]):
            s = ln.lstrip()
            up = s.upper()
            if not (up.startswith("CREATE CACHED TABLE") or up.startswith("CREATE MEMORY TABLE")
                    or up.startswith("CREATE TABLE") or up.startswith("CREATE INDEX")
                    or up.startswith("CREATE UNIQUE INDEX") or up.startswith("ALTER TABLE")
                    or up.startswith("CREATE PRIMARY KEY")):
                continue
            if "SEQUENCE" in up and not up.startswith("CREATE"):
                continue

            def ren(m):
                self._name_counter += 1
                return f'{m.group(1)}"PUBLIC"."HR_{self._name_counter}_{m.group(2)}"'
            stmts.append(_NAME_RE.sub(ren, s))
        self.tconn.commit()
        self._runscript(src, stmts, f"tbl{self._name_counter}")
        self.tconn.commit()

    # -- non-data tables ---------------------------------------------------
    def _copy_rows(self, src: Database, table: str, where_keys: Optional[set] = None) -> int:
        tcols = self._target_columns(table)
        scols_up = {c.upper(): c for c in src.tables[table].column_names}
        cols = [c for c in tcols if c.upper() in scols_up]
        sel = ", ".join(q(scols_up[c.upper()]) for c in cols)
        ins = self.tconn.prepareStatement(
            f"INSERT INTO {q(table)} ({', '.join(q(c) for c in cols)}) VALUES ({', '.join('?' * len(cols))})")
        n = 0
        st = src.conn.createStatement()
        try:
            rs = st.executeQuery(f"SELECT {sel} FROM {q(table)}")
            while rs.next():
                for i in range(1, len(cols) + 1):
                    ins.setObject(i, rs.getObject(i))
                ins.addBatch()
                n += 1
                if n % self.opts.batch_size == 0:
                    ins.executeBatch()
            ins.executeBatch()
        finally:
            st.close()
            ins.close()
        self.tconn.commit()
        return n

    def _copy_other_tables(self) -> None:
        done = set()
        for s in self.sources:
            for t in s.db.tables.values():
                if t.is_data or t.name.upper() == DATA_POINTS or t.name in done:
                    continue
                _check_cancel(self.cancel)
                try:
                    self._ensure_table(s.db, t.name)
                    n = self._copy_rows(s.db, t.name)
                    self.progress(f"Tabelle {t.name}: {n} Zeilen aus {s.db.label} übernommen", None)
                    done.add(t.name)
                except Exception as e:  # noqa: BLE001
                    self.tconn.rollback()
                    s.db.reconnect()
                    self.report.warnings.append(f"Tabelle {t.name} aus {s.db.label} nicht übernommen: "
                                                f"{java_error_text(e)}")

    def _merge_datapoints(self) -> None:
        srcs = [s for s in self.sources if DATA_POINTS in s.db.tables]
        if not srcs:
            self.report.warnings.append("Keine Tabelle DATA_POINTS gefunden.")
            return
        if DATA_POINTS not in self._target_tables():
            self._ensure_table(srcs[0].db, DATA_POINTS)
        tcols = self._target_columns(DATA_POINTS)
        up = [c.upper() for c in tcols]
        keycols = ["INTERFACE", "ADDRESS", "IDENTIFIER"]
        has_key = all(k in up for k in keycols)
        idcol = tcols[up.index("DP_ID")] if "DP_ID" in up else None
        existing_keys: set = set()
        existing_ids: set = set()
        ins = self.tconn.prepareStatement(
            f"INSERT INTO {q(DATA_POINTS)} ({', '.join(q(c) for c in tcols)}) "
            f"VALUES ({', '.join('?' * len(tcols))})")
        total = 0
        for s in srcs:
            if s.db.tables[DATA_POINTS].error:
                self.report.warnings.append(f"DATA_POINTS in {s.db.label} fehlerhaft: "
                                            f"{s.db.tables[DATA_POINTS].error}")
            if not has_key and s is not srcs[0]:
                continue
            added = 0
            for dp in s.db.datapoints:
                key = tuple(str(dp.get(k)) for k in keycols) if has_key else None
                if key is not None and key in existing_keys:
                    continue
                raw = dp.get("_RAW", {})
                row = {c: raw.get(c.upper()) for c in tcols}
                if idcol is not None and row[idcol] is not None:
                    row[idcol] = py(row[idcol])
                    if row[idcol] in existing_ids:
                        new_id = max(existing_ids) + 1
                        self.report.warnings.append(
                            f"DP_ID {row[idcol]} aus {s.db.label} ({key}) bereits vergeben -> neue ID {new_id}")
                        row[idcol] = new_id
                    existing_ids.add(row[idcol])
                for i, c in enumerate(tcols, 1):
                    ins.setObject(i, _for_insert(row[c]))
                ins.addBatch()
                if key is not None:
                    existing_keys.add(key)
                added += 1
            ins.executeBatch()
            total += added
            self.progress(f"DATA_POINTS: {added} Einträge aus {s.db.label}", None)
        ins.close()
        self.tconn.commit()
        if idcol and existing_ids:
            try:
                execute(self.tconn, f"ALTER TABLE {q(DATA_POINTS)} ALTER COLUMN {q(idcol)} "
                                    f"RESTART WITH {max(existing_ids) + 1}")
                self.tconn.commit()
            except Exception as e:  # noqa: BLE001
                self.report.warnings.append(f"Identity von DATA_POINTS.{idcol} nicht gesetzt: {java_error_text(e)}")

    # -- data tables -------------------------------------------------------
    def _has_ts_index(self, table: str, ts_col: str) -> bool:
        md = self.tconn.getMetaData()
        rs = md.getIndexInfo(None, "PUBLIC", table, False, True)
        found = False
        while rs.next():
            if int(rs.getShort("ORDINAL_POSITION")) == 1 and str(rs.getString("COLUMN_NAME")).upper() == ts_col.upper():
                found = True
        rs.close()
        return found

    def _drop_temp_indexes(self) -> None:
        for idx in self._temp_indexes:
            try:
                execute(self.tconn, f"DROP INDEX IF EXISTS {q(idx)}")
            except Exception as e:  # noqa: BLE001
                self.report.warnings.append(f"Temporärer Index {idx} nicht entfernt: {java_error_text(e)}")
        self.tconn.commit()

    def _merge_data(self) -> None:
        order: list[str] = []
        seen = set()
        for s in self.sources:
            for name, t in s.db.tables.items():
                if s.selected(name) and name not in seen:
                    seen.add(name)
                    order.append(name)
        order.sort()
        # estimate total work for progress
        total = 0
        for name in order:
            for s in self.sources:
                if s.selected(name):
                    total += s.db.tables[name].count or 0
        done = [0]
        self._total = max(1, total)
        self._inserted_total = 0
        self._t_data = time.time()
        n_tables = len(order)
        self.progress(f"Übertrage {n_tables} Datenpunkte, bis zu {fmt_n(total)} Werte", 0.05)
        next_pct = 10
        for i, name in enumerate(order):
            _check_cancel(self.cancel)
            pct = int(100 * done[0] / self._total)
            if pct >= next_pct and i:
                self.progress(f"Fortschritt {pct} %: {i}/{n_tables} Datenpunkte, "
                              f"{fmt_n(self._inserted_total)} Werte geschrieben "
                              f"({fmt_dur(time.time() - self._t_data)})", None)
                next_pct = pct + 10
            covered: list[tuple[int, int]] = []
            first = True
            for s in self.sources:
                if not s.selected(name):
                    continue
                try:
                    self._ensure_table(s.db, name)
                except Exception as e:  # noqa: BLE001
                    self.tconn.rollback()
                    st = TransferStat(name, s.db.label, errors=[f"Tabelle nicht anlegbar: {java_error_text(e)}"])
                    self.report.stats.append(st)
                    continue
                st = self._transfer(s, name, first, covered, done, i, n_tables)
                self.report.stats.append(st)
                self._inserted_total += st.inserted
                if st.lost or st.failed:
                    self.progress(f"WARNUNG {name} aus {s.db.label}: {len(st.lost)} unlesbare Bereiche, "
                                  f"{st.failed} Werte nicht schreibbar (Details im Bericht)", None)
                self._expected[name] = self._expected.get(name, 0) + st.inserted
                if st.inserted:
                    first = False
                if self.opts.dedup == "range" and st.min_ms is not None:
                    covered.append((st.min_ms, st.max_ms))

    def _transfer(self, s: SourceSpec, table: str, first: bool, covered: list[tuple[int, int]],
                  done: list[int], ti: int, nt: int) -> TransferStat:
        db = s.db
        t = db.tables[table]
        st = TransferStat(table, db.label)
        tcols = self._target_columns(table)
        scols_up = {c.upper(): c for c in t.column_names}
        cols = [c for c in tcols if c.upper() in scols_up]
        ts_t = next(c for c in cols if c.upper() == TS_COL)
        ts_idx = cols.index(ts_t)
        need_dedup = not first
        if need_dedup and not self._has_ts_index(table, ts_t):
            self._name_counter += 1
            idx = f"HR_TMP_IDX_{self._name_counter}"
            execute(self.tconn, f"CREATE INDEX {q(idx)} ON {q(table)} ({q(ts_t)})")
            self._temp_indexes.append(idx)
        ins = self.tconn.prepareStatement(
            f"INSERT INTO {q(table)} ({', '.join(q(c) for c in cols)}) VALUES ({', '.join('?' * len(cols))})")
        chk = self.tconn.prepareStatement(
            f"SELECT {q(ts_t)} FROM {q(table)} WHERE {q(ts_t)} >= ? AND {q(ts_t)} <= ?")
        lo = s.t_from if s.t_from is not None else t.min_ms
        hi = s.t_to if s.t_to is not None else t.max_ms
        if t.min_ms is not None and lo is not None:
            lo = max(lo, t.min_ms)
        if t.max_ms is not None and hi is not None:
            hi = min(hi, t.max_ms)
        if lo is None or hi is None:
            # stats unknown (defective table) -> try whole possible range
            lo = lo if lo is not None else 0
            hi = hi if hi is not None else int(time.time() * 1000) + 365 * DAY_MS
        if lo > hi:
            ins.close()
            chk.close()
            return st
        reader = RobustReader(db, table, [scols_up[c.upper()] for c in cols], ts_idx,
                              self.opts.rows_per_query, self.opts.min_skip_ms, self.cancel)
        try:
            for batch in reader.batches(lo, hi, t.count):
                st.read += len(batch)
                rows = batch
                if covered:
                    keep = []
                    for r in rows:
                        ms = ts_ms(r[ts_idx])
                        if any(a <= ms <= b for a, b in covered):
                            st.range_skip += 1
                        else:
                            keep.append(r)
                    rows = keep
                if need_dedup and rows:
                    b0 = ts_ms(rows[0][ts_idx])
                    b1 = ts_ms(rows[-1][ts_idx])
                    chk.setObject(1, make_ts(b0))
                    chk.setObject(2, make_ts(b1))
                    rs = chk.executeQuery()
                    have = set()
                    while rs.next():
                        have.add(int(rs.getTimestamp(1).getTime()))
                    rs.close()
                    if have:
                        keep = []
                        for r in rows:
                            if ts_ms(r[ts_idx]) in have:
                                st.dup += 1
                            else:
                                keep.append(r)
                        rows = keep
                if rows:
                    ok = self._insert(ins, rows, st)
                    st.inserted += ok
                    m0, m1 = ts_ms(rows[0][ts_idx]), ts_ms(rows[-1][ts_idx])
                    st.min_ms = m0 if st.min_ms is None else min(st.min_ms, m0)
                    st.max_ms = m1 if st.max_ms is None else max(st.max_ms, m1)
                done[0] += len(batch)
                self.progress(f"Datenpunkt {ti + 1}/{nt} ({table} aus {db.label}): "
                              f"{fmt_n(st.inserted)} übernommen – gesamt "
                              f"{fmt_n(self._inserted_total + st.inserted)} Werte geschrieben",
                              0.05 + 0.9 * min(1.0, done[0] / self._total), True)
        finally:
            ins.close()
            chk.close()
        self.tconn.commit()
        st.lost = reader.lost
        st.errors += reader.errors
        return st

    def _insert(self, ins, rows: list[tuple], st: TransferStat) -> int:
        try:
            for r in rows:
                for i, v in enumerate(r, 1):
                    ins.setObject(i, v)
                ins.addBatch()
            ins.executeBatch()
            return len(rows)
        except Exception as e:  # noqa: BLE001
            ins.clearBatch()
            self.tconn.rollback()
            st.errors.append(f"Batch-Insert fehlgeschlagen, einzeln: {java_error_text(e)}")
            ok = 0
            for r in rows:
                try:
                    for i, v in enumerate(r, 1):
                        ins.setObject(i, v)
                    ins.executeUpdate()
                    ok += 1
                except Exception as e2:  # noqa: BLE001
                    st.failed += 1
                    if len(st.errors) < 20:
                        st.errors.append(java_error_text(e2))
            return ok

    # -- verification ------------------------------------------------------
    def _verify(self, user: str, pw: str) -> None:
        self.progress(f"Daten geschrieben: {fmt_n(self._inserted_total)} Werte in "
                      f"{fmt_dur(time.time() - self._t_data)}. Verifiziere neue Datenbank ...", 0.98)
        conn = self.tdrv.connect(f"jdbc:h2:file:{self.tbase};IFEXISTS=TRUE", user, pw)
        try:
            for k, (table, exp) in enumerate(self._expected.items()):
                if k % 20 == 0:
                    self.progress(f"Verifiziere Tabelle {k + 1}/{len(self._expected)}", None, True)
                try:
                    got = int(py(query_all(conn, f"SELECT COUNT(*) FROM {q(table)}")[0][0]))
                except Exception as e:  # noqa: BLE001
                    got = -1
                    self.report.warnings.append(f"Verifikation {table}: {java_error_text(e)}")
                self.report.verify[table] = (exp, got)
        finally:
            conn.close()


def _for_insert(v):
    """DATA_POINTS values were converted to python for display; pass through."""
    return v


# ---------------------------------------------------------------------------
# Fault tolerant reader
# ---------------------------------------------------------------------------

class RobustReader:
    """Reads rows of a data table ordered by timestamp in time windows.

    A window that raises an error is retried from the last successfully read
    timestamp; if no progress is possible it is bisected. Windows smaller than
    min_skip_ms that still fail are given up and recorded in ``lost``.
    """

    def __init__(self, db: Database, table: str, cols: list[str], ts_idx: int,
                 rows_per_query: int, min_skip_ms: int, cancel: Optional[threading.Event]):
        self.db = db
        self.table = table
        self.cols = cols
        self.ts_idx = ts_idx
        self.rows_per_query = max(1000, rows_per_query)
        self.min_skip_ms = max(1, min_skip_ms)
        self.cancel = cancel
        self.lost: list[tuple[int, int]] = []
        self.errors: list[str] = []
        tc = q(cols[ts_idx])
        self.sql = (f"SELECT {', '.join(q(c) for c in cols)} FROM {q(table)} "
                    f"WHERE {tc} >= ? AND {tc} <= ? ORDER BY {tc}")

    def _windows(self, lo: int, hi: int, count: Optional[int]) -> list[tuple[int, int]]:
        span = hi - lo + 1
        if count and count > self.rows_per_query:
            n = min(100_000, -(-count // self.rows_per_query))
            step = max(60_000, span // n + 1)
        else:
            step = span
        out = []
        a = lo
        while a <= hi:
            b = min(hi, a + step - 1)
            out.append((a, b))
            a = b + 1
        return out

    def _add_lost(self, a: int, b: int) -> None:
        if self.lost and self.lost[-1][1] + 1 >= a:
            self.lost[-1] = (self.lost[-1][0], max(b, self.lost[-1][1]))
        else:
            self.lost.append((a, b))

    def batches(self, lo: int, hi: int, count: Optional[int] = None, batch: int = 5000):
        pending = self._windows(lo, hi, count)
        ncols = len(self.cols)
        ti = self.ts_idx + 1
        while pending:
            _check_cancel(self.cancel)
            a, b = pending.pop(0)
            last: Optional[int] = None
            buf: list[tuple] = []
            try:
                ps = self.db.conn.prepareStatement(self.sql)
                try:
                    ps.setFetchSize(batch)
                    ps.setTimestamp(1, make_ts(a))
                    ps.setTimestamp(2, make_ts(b))
                    rs = ps.executeQuery()
                    while rs.next():
                        row = tuple(rs.getTimestamp(i) if i == ti else rs.getObject(i)
                                    for i in range(1, ncols + 1))
                        buf.append(row)
                        if len(buf) >= batch:
                            last = ts_ms(buf[-1][self.ts_idx])
                            out, buf = buf, []
                            yield out
                            _check_cancel(self.cancel)
                    rs.close()
                finally:
                    try:
                        ps.close()
                    except Exception:  # noqa: BLE001
                        pass
                if buf:
                    yield buf
            except Cancelled:
                raise
            except GeneratorExit:
                raise
            except Exception as e:  # noqa: BLE001
                if len(self.errors) < 50:
                    self.errors.append(f"{ms_to_str(a)}..{ms_to_str(b)}: {java_error_text(e)}")
                log.info("read error %s %s..%s: %s", self.table, ms_to_str(a), ms_to_str(b),
                            java_error_text(e))
                if buf:
                    last = ts_ms(buf[-1][self.ts_idx])
                    yield buf
                self.db.reconnect()
                start = a if last is None else last + 1
                if start > b:
                    continue
                if last is not None:
                    pending.insert(0, (start, b))
                elif b - start + 1 <= self.min_skip_ms:
                    self._add_lost(start, b)
                else:
                    mid = start + (b - start) // 2
                    pending[0:0] = [(start, mid), (mid + 1, b)]


def write_report(report: MergeReport, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(report.text())
        f.write("\n")
    with open(os.path.splitext(path)[0] + ".json", "w", encoding="utf-8") as f:
        json.dump({
            "output": report.output_path, "ok": report.ok, "duration_s": report.duration_s,
            "warnings": report.warnings, "sources_unchanged": report.sources_unchanged,
            "verify": report.verify,
            "stats": [s.__dict__ for s in report.stats],
        }, f, indent=1, default=str)
