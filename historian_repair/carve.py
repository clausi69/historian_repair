"""Raw page carving for H2 MVStore files (written by H2 1.4.200).

Used when a database cannot be opened, or opens with an old/empty state
because H2 rolled back to an ancient chunk (store header damaged, file
truncated). The carver does not rely on the B-tree structure:

1. All chunk headers found at block boundaries are collected. Each one is
   tried as "last chunk" (via reflection) to read its meta map, which gives
   map id -> map name and further chunk locations.
2. Every chunk range is scanned for page starts; a page start is recognised
   by its check value (derived from chunk id, offset and length).
3. Leaf pages of table maps ("table.<objectId>") are decoded with H2's own
   ValueDataType / VersionedValueType classes.
4. H2's internal meta table (map "table.0") provides the CREATE statements,
   so tables are recreated with their original definition and names.
5. Everything is written into a NEW H2 database. Rows found in several page
   versions are de-duplicated by row key (newest chunk version wins).

The source file is only read (from a copy).
"""
from __future__ import annotations

import os
import re
import shutil
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from . import h2engine as E

BS = 4096


@dataclass
class MvChunk:
    id: int
    block: int
    len: int
    version: int


@dataclass
class MvAnalysis:
    path: str
    blocks: int
    header: dict
    header_version: Optional[int]
    chunks: list[MvChunk] = field(default_factory=list)  # chunk headers found at block starts

    @property
    def max_chunk_version(self) -> Optional[int]:
        return max((c.version for c in self.chunks), default=None)

    @property
    def rollback_suspected(self) -> bool:
        mv = self.max_chunk_version
        return (self.header_version is not None and mv is not None and mv > self.header_version)

    def text(self) -> str:
        t = (f"{len(self.chunks)} Chunk-Header in {self.blocks} Blöcken, Header-Version "
             f"{self.header_version:x}" if self.header_version is not None else "Header unlesbar")
        if self.max_chunk_version is not None:
            t += f", neueste Chunk-Version in Datei {self.max_chunk_version:x}"
        if self.rollback_suspected:
            t += (" – WARNUNG: Datei enthält neuere Chunks als der Store-Header. H2 öffnet dann einen "
                  "alten Stand (Rollback). Roh-Extraktion (Carving) empfohlen.")
        return t


def _parse_kv(line: str) -> dict:
    return dict(p.split(":", 1) for p in line.split(",") if ":" in p)


def analyze_mvstore(path: str, cancel: Optional[threading.Event] = None) -> MvAnalysis:
    """Cheap structural scan: header + all chunk headers at block boundaries."""
    size = os.path.getsize(path)
    hdr: dict = {}
    chunks: list[MvChunk] = []
    with open(path, "rb") as f:
        head = f.read(BS)
        try:
            hdr = _parse_kv(head.split(b"\n", 1)[0].decode("latin-1"))
        except Exception:  # noqa: BLE001
            hdr = {}
        f.seek(0)
        blk = 0
        step = 16 * 1024 * 1024
        while True:
            if cancel is not None and cancel.is_set():
                raise E.Cancelled()
            buf = f.read(step)
            if not buf:
                break
            for o in range(0, len(buf), BS):
                if buf[o:o + 6] == b"chunk:":
                    line = buf[o:o + 1024].split(b"\n", 1)[0].decode("latin-1", "replace")
                    try:
                        h = _parse_kv(line.strip())
                        b = int(h["block"], 16)
                        if b == blk + o // BS:
                            chunks.append(MvChunk(int(h["chunk"], 16), b, int(h["len"], 16),
                                                  int(h["version"], 16)))
                    except (KeyError, ValueError):
                        pass
            blk += len(buf) // BS
    hv = None
    try:
        hv = int(hdr["version"], 16)
    except (KeyError, ValueError):
        pass
    return MvAnalysis(path=path, blocks=size // BS, header=hdr, header_version=hv, chunks=chunks)


# ---------------------------------------------------------------------------

@dataclass
class CarveReport:
    output: str = ""
    maps_known: int = 0
    pages_found: int = 0
    pages_decoded: int = 0
    pages_failed: int = 0
    tables: dict = field(default_factory=dict)       # name -> (rows, min_ms, max_ms)
    statements_failed: list = field(default_factory=list)
    unnamed_tables: list = field(default_factory=list)
    debug: dict = field(default_factory=dict)

    def text(self, max_tables: Optional[int] = None) -> str:
        L = [f"Carving-Ergebnis: {self.output}",
             f"Maps bekannt: {self.maps_known}, Seiten gefunden: {self.pages_found}, "
             f"Tabellen-Blattseiten dekodiert: {self.pages_decoded}, fehlgeschlagen: {self.pages_failed}",
             f"Tabellen mit Daten: {len(self.tables)}, Zeilen gesamt: "
             f"{sum(v[0] for v in self.tables.values())}"]
        items = sorted(self.tables.items(), key=lambda x: -x[1][0])
        shown = items if max_tables is None else items[:max_tables]
        if max_tables is not None and len(items) > max_tables:
            L.append(f"  größte {max_tables} Tabellen (alle in carve_report.txt):")
        for n, (cnt, a, b) in shown:
            L.append(f"  {n:55s} {cnt:8d}  {E.ms_to_str(a)} .. {E.ms_to_str(b)}")
        if self.unnamed_tables:
            u = self.unnamed_tables
            L.append(f"{len(u)} Tabellen ohne gefundene Definition (als CARVED_* angelegt): "
                     + ", ".join(u[:8]) + (f", … (+{len(u) - 8})" if len(u) > 8 else ""))
        if self.statements_failed:
            L.append("Nicht ausführbare DDL-Anweisungen:")
            L += [f"  {s}" for s in self.statements_failed[:30]]
        return "\n".join(L)


def _cv(x):
    import numpy as np
    x = np.asarray(x, dtype=np.int64)
    return ((x >> 16) ^ x) & 0xFFFF


def _scan_pages(arr, start: int, end: int, cid: int):
    """Return [(file_pos, offset, length)] of page starts inside a chunk range."""
    import numpy as np
    seg = arr[start:end].astype(np.int64)
    n = len(seg) - 6
    if n <= 0:
        return []
    plen = (seg[0:n] << 24) | (seg[1:n + 1] << 16) | (seg[2:n + 2] << 8) | seg[3:n + 3]
    plen = np.where(plen >= 2 ** 31, plen - 2 ** 32, plen)
    chk = (seg[4:n + 4] << 8) | seg[5:n + 5]
    off = np.arange(n, dtype=np.int64)
    exp = (_cv(cid) ^ _cv(off) ^ _cv(plen)) & 0xFFFF
    ok = (plen >= 12) & (plen <= (len(seg) - off)) & (chk == exp)
    return [(start + int(o), int(o), int(plen[o])) for o in np.nonzero(ok)[0]]


_TYPE_ORDER = {3: 0, 9: 1, 0: 2, 1: 3, 5: 4}   # sequence, alias, table, index, constraint


def carve_database(file_path: str, jar: str, workdir: str, user: str = "sa", password: str = "",
                   progress: E.ProgressCb = E._noop_progress,
                   cancel: Optional[threading.Event] = None) -> tuple[str, CarveReport]:
    import jpype
    import numpy as np

    info = E.inspect_db_file(file_path)
    if info.kind != "mv" or info.fmt != 1:
        raise RuntimeError("Carving unterstützt derzeit nur MVStore-Dateien im Format 1 (H2 1.4.x).")
    drv = E.H2Driver.get(jar)
    if not drv.version.startswith("1.4.200"):
        raise RuntimeError("Carving benötigt das JAR h2-1.4.200 (Dekodierung der Seiten).")
    L = drv.loader

    def J(n):
        return jpype.JClass(n, loader=L)

    rdir = E.unique_dir(workdir, "carve")
    src = os.path.join(rdir, "source_copy.mv.db")
    progress(f"Carving: kopiere {file_path} ({E.fmt_mb(info.size)})", None)
    E.copy_with_progress(info.path, src, progress, "Carving: kopiere", cancel)
    rep = CarveReport()

    # -- 1. chunks + meta ----------------------------------------------------
    progress("Carving: suche Chunks ...", 0.02)
    ana = analyze_mvstore(src, cancel)
    with open(src, "rb") as f:
        data = f.read()
    nblocks = len(data) // BS
    MV = J("org.h2.mvstore.MVStore")
    Chunk = J("org.h2.mvstore.Chunk")
    Long = jpype.JClass("java.lang.Long")
    Integer = jpype.JClass("java.lang.Integer")
    store = J("org.h2.mvstore.MVStore$Builder")().fileName(src).readOnly().recoveryMode().open()

    def priv(name, *types):
        m = MV.class_.getDeclaredMethod(name, *types)
        m.setAccessible(True)
        return m

    rch = priv("readChunkHeaderAndFooter", jpype.JLong.class_, jpype.JInt.class_)
    setlast = priv("setLastChunk", Chunk.class_)
    maps: dict[int, str] = {}
    chunk_ranges: dict[tuple[int, int], tuple[int, int]] = {}   # (id, block) -> (len, version)
    for c in ana.chunks:
        chunk_ranges[(c.id, c.block)] = (c.len, c.version)
    for i, c in enumerate(sorted(ana.chunks, key=lambda c: c.version)):
        E._check_cancel(cancel)
        try:
            jc = rch.invoke(store, Long(c.block), Integer(c.id))
            if jc is None:
                continue
            setlast.invoke(store, jc)
            meta = store.getMetaMap()
            for k in meta.keySet():
                k = str(k)
                v = str(meta.get(k))
                if k.startswith("map."):
                    name = _parse_kv(v).get("name")
                    if name:
                        maps[int(k[4:], 16)] = name
                elif k.startswith("chunk."):
                    ch = _parse_kv(v)
                    b = int(ch["block"], 16)
                    if b < nblocks:
                        key = (int(ch["chunk"], 16), b)
                        ver = int(ch.get("version", "0"), 16)
                        if key not in chunk_ranges or chunk_ranges[key][1] < ver:
                            chunk_ranges[key] = (int(ch["len"], 16), ver)
        except Exception:  # noqa: BLE001
            continue
    store.closeImmediately()
    rep.maps_known = len(maps)
    progress(f"Carving: {len(maps)} Maps, {len(chunk_ranges)} Chunk-Bereiche", 0.1)

    # -- 2. page scan ----------------------------------------------------------
    arr = np.frombuffer(data, dtype=np.uint8)
    pages: dict[int, tuple[int, int, int]] = {}    # file pos -> (offset, length, version)
    for (cid, blk), (ln, ver) in chunk_ranges.items():
        start = blk * BS
        end = min(len(data), (blk + ln) * BS)
        for fp, off, plen in _scan_pages(arr, start, end, cid):
            if fp not in pages or pages[fp][2] < ver:
                pages[fp] = (off, plen, ver)
    rep.pages_found = len(pages)
    progress(f"Carving: {len(pages)} Seiten gefunden, dekodiere ...", 0.2)

    # -- 3. decode -------------------------------------------------------------
    ByteBuffer = jpype.JClass("java.nio.ByteBuffer")
    DU = J("org.h2.mvstore.DataUtils")
    VDT = J("org.h2.mvstore.db.ValueDataType")
    VVT = J("org.h2.mvstore.tx.VersionedValueType")
    lzf = J("org.h2.compress.CompressLZF")()
    deflate = J("org.h2.compress.CompressDeflate")()
    kt, vt = VDT(), VVT(VDT())
    ObjArr = jpype.JArray(jpype.JObject)
    jdata = jpype.JArray(jpype.JByte)(data)
    rows: dict[int, dict[int, tuple[int, list]]] = {}   # map id -> {rowkey: (version, values)}
    index_rows: dict[int, set] = {}                      # index map id -> {(ts_ms, rowkey)}
    items = sorted(pages.items())
    for i, (fp, (off, plen, ver)) in enumerate(items):
        if i % 2000 == 0:
            E._check_cancel(cancel)
            progress(f"Carving: dekodiere Seite {E.fmt_n(i)} von {E.fmt_n(len(items))}, "
                     f"{E.fmt_n(sum(len(r) for r in rows.values()))} Zeilen gefunden",
                     0.2 + 0.5 * i / max(1, len(items)), True)
        buf = ByteBuffer.wrap(jdata, fp, plen)
        buf.getInt()
        buf.getShort()
        try:
            mid = int(DU.readVarInt(buf))
            n = int(DU.readVarInt(buf))
            typ = int(buf.get())
        except Exception:  # noqa: BLE001
            continue
        if typ & 1:
            continue  # inner node
        name = maps.get(mid)
        if name is not None and not (name.startswith("table.") or name.startswith("index.")):
            continue
        try:
            if typ & 2:
                add = int(DU.readVarInt(buf))
                comp = jpype.JArray(jpype.JByte)(int(buf.remaining()))
                buf.get(comp)
                total = len(comp) + add
                out = jpype.JArray(jpype.JByte)(total)
                (deflate if (typ & 6) == 6 else lzf).expand(comp, 0, len(comp), out, 0, total)
                buf = ByteBuffer.wrap(out)
            keys, vals = ObjArr(n), ObjArr(n)
            kt.read(buf, keys, n, True)
            vt.read(buf, vals, n, False)
            idx_keys = _index_keys(keys)
            if idx_keys is not None:
                if name is None or name.startswith("index."):
                    index_rows.setdefault(mid, set()).update(idx_keys)
                continue
            if name is not None and name.startswith("index."):
                continue
            decoded = []
            for k, v in zip(keys, vals):
                cur = v.getCurrentValue() if v is not None else None
                if cur is None:
                    continue
                decoded.append((int(k.getLong()), [x for x in cur.getList()]))
        except Exception:  # noqa: BLE001
            if name is not None and name.startswith("table."):
                rep.pages_failed += 1
            continue
        if name is None:
            # unknown map: accept only if it looks like historian data (TS, VALUE, STATE)
            if not decoded or len(decoded[0][1]) != 3 or "Timestamp" not in str(decoded[0][1][0].getClass()):
                continue
        rep.pages_decoded += 1
        tgt = rows.setdefault(mid, {})
        for k, vals_ in decoded:
            old = tgt.get(k)
            if old is None or old[0] < ver:
                tgt[k] = (ver, vals_)

    # -- 4. schema from H2 meta table ------------------------------------------
    meta_map = next((m for m, nm in maps.items() if nm == "table.0"), None)
    records = {}   # object id -> (type, sql)
    if meta_map is not None and meta_map in rows:
        for _, (_, v) in rows.pop(meta_map).items():
            try:
                records[int(v[0].getInt())] = (int(v[2].getInt()), str(v[3].getString()))
            except Exception:  # noqa: BLE001
                pass
    progress("Carving: suche SQL-Definitionen in Rohdaten ...", 0.72)
    for oid, rec in _carve_meta_records(data, jdata, ByteBuffer, kt).items():
        records.setdefault(oid, rec)
    table_sql: dict[int, tuple[str, str]] = {}
    index_table: dict[int, str] = {}     # index object id -> table name
    for oid, (typ, sql) in records.items():
        if typ == 0:
            m = re.match(r'CREATE (?:CACHED |MEMORY )?TABLE (?:IF NOT EXISTS )?"?PUBLIC"?\."?([^"(\s]+)"?', sql)
            if m:
                table_sql[oid] = (m.group(1), sql)
        elif typ == 1:
            m = re.search(r' ON "?PUBLIC"?\."?([^"(\s]+)"?', sql)
            if m:
                index_table[oid] = m.group(1)
    name_by_table_sql = {n: sql for n, sql in table_sql.values()}
    rep.debug = {"records": len(records), "table_sql": len(table_sql), "index_sql": len(index_table),
                 "index_maps": len(index_rows), "index_keys": sum(len(v) for v in index_rows.values()),
                 "index_maps_named": sum(1 for m in index_rows if maps.get(m))}

    def resolve(mid: int, r: dict) -> Optional[str]:
        name = maps.get(mid)
        if name and name.startswith("table."):
            oid = int(name.split(".", 1)[1])
            if oid in table_sql:
                return table_sql[oid][0]
        # link via secondary index (TS, rowkey)
        sample = []
        for k, (_, v) in r.items():
            try:
                sample.append((E.ts_ms(v[0].getObject()), k))
            except Exception:  # noqa: BLE001
                break
            if len(sample) >= 200:
                break
        if not sample:
            return None
        best, hits = None, 0
        for imid, keys in index_rows.items():
            h = sum(1 for x in sample if x in keys)
            if h > hits:
                best, hits = imid, h
        if best is None or hits < max(1, len(sample) // 2):
            return None
        iname = maps.get(best)
        if iname and iname.startswith("index."):
            return index_table.get(int(iname.split(".", 1)[1]))
        return None

    # -- 5. write new database ---------------------------------------------------
    newbase = os.path.join(rdir, "carved")
    progress("Carving: schreibe neue Datenbank ...", 0.75)
    conn = drv.connect(f"jdbc:h2:file:{newbase}", user, password)
    try:
        ordered = sorted(((t, oid, sql) for oid, (t, sql) in records.items() if t in _TYPE_ORDER),
                         key=lambda x: (_TYPE_ORDER[x[0]], x[1]))
        for t, oid, sql in ordered:
            try:
                E.execute(conn, sql)
            except Exception as e:  # noqa: BLE001
                rep.statements_failed.append(f"{sql[:120]} -> {E.java_error_text(e)[:120]}")
        conn.setAutoCommit(False)
        groups: dict[str, list[dict]] = {}
        for mid, r in sorted(rows.items()):
            if not r:
                continue
            tname = resolve(mid, r)
            if tname is None:
                name = maps.get(mid)
                oid = int(name.split(".", 1)[1]) if name and name.startswith("table.") else None
                tname = f"CARVED_{'T' + str(oid) if oid is not None else 'MAP' + str(mid)}"
                rep.unnamed_tables.append(tname)
            elif tname not in _existing_tables(conn):
                sql = name_by_table_sql.get(tname)
                if sql:
                    try:
                        E.execute(conn, sql)
                    except Exception as e:  # noqa: BLE001
                        rep.statements_failed.append(f"{sql[:120]} -> {E.java_error_text(e)[:120]}")
            groups.setdefault(tname, []).append(r)
        n_groups = len(groups)
        total_rows = sum(len(r) for parts in groups.values() for r in parts)
        written = 0
        progress(f"Carving: schreibe {n_groups} Tabellen mit bis zu {E.fmt_n(total_rows)} Werten", 0.75)
        for gi, (tname, parts) in enumerate(sorted(groups.items())):
            E._check_cancel(cancel)
            r = _merge_parts(parts)
            if tname not in _existing_tables(conn):
                first = next(iter(r.values()))[1]
                coldefs = ", ".join(f'"C{i}" {_sql_type(v)}' for i, v in enumerate(first))
                if len(first) == 3:
                    coldefs = f'"TS" TIMESTAMP, "VALUE" {_sql_type(first[1])}, "STATE" INT'
                E.execute(conn, f'CREATE TABLE IF NOT EXISTS {E.q(tname)} ({coldefs})')
            ncols = len(next(iter(r.values()))[1])
            ps = conn.prepareStatement(f"INSERT INTO {E.q(tname)} VALUES ({', '.join('?' * ncols)})")
            cnt = 0
            mn = mx = None
            try:
                for _, (_, vals_) in sorted(r.items()):
                    if len(vals_) != ncols:
                        continue
                    for i, v in enumerate(vals_, 1):
                        ps.setObject(i, v.getObject())
                    ps.addBatch()
                    cnt += 1
                    if cnt % 5000 == 0:
                        ps.executeBatch()
                        progress(f"Carving: schreibe Tabelle {gi + 1}/{n_groups} ({tname}), "
                                 f"{E.fmt_n(written + cnt)} von {E.fmt_n(total_rows)} Werten geschrieben",
                                 0.75 + 0.2 * (written + cnt) / max(1, total_rows), True)
                ps.executeBatch()
                conn.commit()
                written += cnt
                progress(f"Carving: schreibe Tabelle {gi + 1}/{n_groups} ({tname}), "
                         f"{E.fmt_n(written)} von {E.fmt_n(total_rows)} Werten geschrieben",
                         0.75 + 0.2 * written / max(1, total_rows), True)
            except Exception as e:  # noqa: BLE001
                conn.rollback()
                rep.statements_failed.append(f"INSERT {tname}: {E.java_error_text(e)[:150]}")
                cnt = 0
            finally:
                ps.close()
            if cnt:
                try:
                    first_v = next(iter(r.values()))[1][0]
                    if "Timestamp" in str(first_v.getClass()):
                        ts = [E.ts_ms(v[1][0].getObject()) for v in r.values()]
                        mn, mx = min(ts), max(ts)
                except Exception:  # noqa: BLE001
                    pass
                rep.tables[tname] = (cnt, mn, mx)
        conn.setAutoCommit(True)
        progress(f"Carving: {E.fmt_n(written)} Werte geschrieben, kompaktiere ...", 0.95)
        with E.Ticker(progress, "Carving: kompaktiere neue Datenbank", rdir):
            E.execute(conn, "SHUTDOWN COMPACT")
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    os.remove(src)
    rep.output = newbase + ".mv.db"
    with open(os.path.join(rdir, "carve_report.txt"), "w", encoding="utf-8") as f:
        f.write(rep.text() + "\n")
    progress(f"Carving: fertig – {len(rep.tables)} Tabellen, {E.fmt_n(sum(v[0] for v in rep.tables.values()))} "
             f"Werte gerettet", 1.0)
    return rep.output, rep


def _sql_type(v) -> str:
    cn = str(v.getClass().getSimpleName())
    return {"ValueTimestamp": "TIMESTAMP", "ValueDouble": "DOUBLE", "ValueInt": "INT", "ValueLong": "BIGINT",
            "ValueString": "VARCHAR", "ValueBoolean": "BOOLEAN", "ValueNull": "VARCHAR"}.get(cn, "VARCHAR")


def _existing_tables(conn) -> set:
    return {str(r[0]) for r in E.query_all(conn, "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
                                                 "WHERE TABLE_SCHEMA = 'PUBLIC'")}


def _merge_parts(parts: list[dict]) -> dict:
    """Merge rows of several maps belonging to one table; de-duplicate equal rows."""
    if len(parts) == 1:
        return parts[0]
    out, seen, n = {}, set(), 0
    for r in parts:
        for _, (ver, vals) in sorted(r.items()):
            key = tuple(str(v.toString()) for v in vals)
            if key in seen:
                continue
            seen.add(key)
            out[n] = (ver, vals)
            n += 1
    return out


def _index_keys(keys) -> Optional[set]:
    """If the page keys look like secondary index keys [TS, rowkey], return {(ts_ms, rowkey)}."""
    res = set()
    for k in keys:
        if k is None or "ValueArray" not in str(k.getClass().getSimpleName()):
            return None
        lst = k.getList()
        if len(lst) != 2 or "ValueTimestamp" not in str(lst[0].getClass().getSimpleName()):
            return None
        res.add((E.ts_ms(lst[0].getObject()), int(lst[1].getLong())))
    return res


def _carve_meta_records(data: bytes, jdata, ByteBuffer, kt) -> dict:
    """Find H2 meta rows [ID, HEAD, TYPE, SQL] directly in the raw bytes."""
    out = {}
    for m in re.finditer(rb"CREATE (?:CACHED TABLE|MEMORY TABLE|TABLE|INDEX|UNIQUE INDEX|PRIMARY KEY)", data):
        p = m.start()
        for back in range(4, 40):
            st = p - back
            if st < 0:
                break
            try:
                buf = ByteBuffer.wrap(jdata, st, min(len(data) - st, 70000))
                v = kt.read(buf)
                if "ValueArray" not in str(v.getClass().getSimpleName()):
                    continue
                lst = v.getList()
                if len(lst) != 4:
                    continue
                sql = str(lst[3].getString())
                if not sql.startswith("CREATE"):
                    continue
                out[int(lst[0].getInt())] = (int(lst[2].getInt()), sql)
                break
            except Exception:  # noqa: BLE001
                continue
    return out
