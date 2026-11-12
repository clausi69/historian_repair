"""Tkinter user interface."""
from __future__ import annotations

import json
import os
import queue
import string
import threading
import traceback
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from typing import Callable, Optional

from . import __version__
from . import h2engine as E

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JAR_DIR = os.path.join(APP_DIR, "jars")
SETTINGS_FILE = os.path.join(os.path.expanduser("~"), ".historian_repair.json")
CHECK_ON, CHECK_OFF = "☑", "☐"
COLORS = ["#1f77b4", "#d62728", "#2ca02c", "#9467bd", "#ff7f0e", "#8c564b"]


# ---------------------------------------------------------------------------
# Background worker (all JDBC access happens in this single thread)
# ---------------------------------------------------------------------------

class Worker:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.tasks: "queue.Queue" = queue.Queue()
        self.events: "queue.Queue" = queue.Queue()
        self.cancel = threading.Event()
        self.busy = False
        self.on_busy: Callable[[bool], None] = lambda b: None
        self.on_progress: Callable[[str, Optional[float]], None] = lambda m, f: None
        threading.Thread(target=self._loop, daemon=True).start()
        root.after(100, self._poll)

    def submit(self, fn, on_done=None, on_error=None, title=""):
        self.tasks.put((fn, on_done, on_error, title))

    def progress(self, msg: str, frac: Optional[float] = None) -> None:
        self.events.put(("progress", msg, frac))

    def _loop(self):
        while True:
            fn, on_done, on_error, title = self.tasks.get()
            self.cancel.clear()
            self.events.put(("busy", True, title))
            try:
                res = fn()
                self.events.put(("done", on_done, res))
            except BaseException as e:  # noqa: BLE001
                tb = traceback.format_exc()
                self.events.put(("error", on_error, (e, tb)))
            finally:
                self.events.put(("busy", self.tasks.qsize() > 0, ""))

    def _poll(self):
        try:
            while True:
                ev = self.events.get_nowait()
                kind = ev[0]
                if kind == "progress":
                    self.on_progress(ev[1], ev[2])
                elif kind == "busy":
                    self.busy = ev[1]
                    self.on_busy(ev[1])
                    if ev[1] and ev[2]:
                        self.on_progress(ev[2], None)
                elif kind == "done":
                    if ev[1]:
                        ev[1](ev[2])
                elif kind == "error":
                    e, tb = ev[2]
                    if ev[1]:
                        ev[1](e, tb)
        except queue.Empty:
            pass
        self.root.after(100, self._poll)


# ---------------------------------------------------------------------------
# Settings / jars
# ---------------------------------------------------------------------------

def load_settings() -> dict:
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_settings(s: dict) -> None:
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(s, f, indent=1)
    except OSError:
        pass


class JarRegistry:
    def __init__(self, extra: list[str]):
        self.paths: dict[str, str] = {}   # display -> path
        self.versions: dict[str, Optional[str]] = {}
        self.extra = [p for p in extra if os.path.isfile(p)]
        self.refresh()

    def refresh(self):
        cands = []
        if os.path.isdir(JAR_DIR):
            cands += [os.path.join(JAR_DIR, f) for f in sorted(os.listdir(JAR_DIR)) if f.endswith(".jar")]
        cands += self.extra
        self.paths.clear()
        for p in cands:
            v = E.jar_version(p)
            if v is None:
                continue
            disp = f"H2 {v}  ({os.path.basename(p)})"
            if disp in self.paths and self.paths[disp] != p:
                disp = f"H2 {v}  ({p})"
            self.paths[disp] = p
            self.versions[p] = v

    def add(self, path: str) -> Optional[str]:
        path = os.path.abspath(path)
        if E.jar_version(path) is None:
            return None
        if path not in self.extra:
            self.extra.append(path)
        self.refresh()
        return self.display_for(path)

    def display_for(self, path: Optional[str]) -> str:
        for d, p in self.paths.items():
            if p == path:
                return d
        return ""

    def best_for_format(self, fmt: Optional[int], kind: str = "mv") -> Optional[str]:
        best = None
        for d, p in self.paths.items():
            v = self.versions.get(p)
            if kind == "page":
                if E.is_v1(v):
                    best = d
                continue
            if fmt is not None and E.format_for_version(v) == fmt:
                best = d   # last one = highest version (sorted by name)
        return best

    def newest(self) -> Optional[str]:
        def key(d):
            v = self.versions.get(self.paths[d]) or "0"
            return [int(x) if x.isdigit() else 0 for x in v.split(".")]
        return max(self.paths, key=key) if self.paths else None


# ---------------------------------------------------------------------------
# Source slot
# ---------------------------------------------------------------------------

class SourceSlot:
    def __init__(self, app: "App", label: str):
        self.app = app
        self.label = label
        self.db: Optional[E.Database] = None
        self.selected: set[str] = set()
        self.daily: dict[str, list] = {}
        self.path = tk.StringVar()
        self.jar = tk.StringVar()
        self.user = tk.StringVar(value="sa")
        self.password = tk.StringVar(value="")
        self.t_from = tk.StringVar()
        self.t_to = tk.StringVar()
        self.active = tk.BooleanVar(value=True)
        self.info = tk.StringVar(value="nicht geöffnet")
        self.frame: Optional[ttk.Frame] = None

    @property
    def color(self) -> str:
        return COLORS[(ord(self.label) - 65) % len(COLORS)]

    def build(self, parent) -> ttk.Frame:
        f = ttk.Frame(parent, padding=(4, 3), relief="groove", borderwidth=1)
        self.frame = f
        lab = tk.Label(f, text=f" {self.label} ", fg="white", bg=self.color, font=("TkDefaultFont", 11, "bold"))
        lab.grid(row=0, column=0, rowspan=2, sticky="ns", padx=(0, 4))
        ttk.Entry(f, textvariable=self.path, width=55).grid(row=0, column=1, sticky="ew")
        ttk.Button(f, text="…", width=3, command=self.browse).grid(row=0, column=2)
        ttk.Label(f, text="JAR").grid(row=0, column=3, padx=(6, 1))
        self.jar_cb = ttk.Combobox(f, textvariable=self.jar, width=30, state="readonly",
                                   values=list(self.app.jars.paths))
        self.jar_cb.grid(row=0, column=4)
        ttk.Label(f, text="Benutzer").grid(row=0, column=5, padx=(6, 1))
        ttk.Entry(f, textvariable=self.user, width=6).grid(row=0, column=6)
        ttk.Label(f, text="Passwort").grid(row=0, column=7, padx=(6, 1))
        ttk.Entry(f, textvariable=self.password, width=8, show="•").grid(row=0, column=8)
        bf = ttk.Frame(f)
        bf.grid(row=0, column=9, padx=(6, 0))
        ttk.Button(bf, text="Öffnen", command=self.open).pack(side="left")
        ttk.Button(bf, text="Prüfen", command=self.deep_check).pack(side="left")
        ttk.Button(bf, text="Recover", command=self.recover).pack(side="left")
        ttk.Button(bf, text="Carving", command=self.carve).pack(side="left")
        ttk.Button(bf, text="▲", width=2, command=lambda: self.app.move_slot(self, -1)).pack(side="left")
        ttk.Button(bf, text="▼", width=2, command=lambda: self.app.move_slot(self, 1)).pack(side="left")
        ttk.Button(bf, text="✕", width=2, command=lambda: self.app.remove_slot(self)).pack(side="left")

        r = ttk.Frame(f)
        r.grid(row=1, column=1, columnspan=9, sticky="ew", pady=(3, 0))
        ttk.Checkbutton(r, text="in neue DB übernehmen", variable=self.active,
                        command=self.app.redraw_timeline).pack(side="left")
        ttk.Label(r, text="   Zeitraum von").pack(side="left")
        e1 = ttk.Entry(r, textvariable=self.t_from, width=19)
        e1.pack(side="left", padx=2)
        ttk.Label(r, text="bis").pack(side="left")
        e2 = ttk.Entry(r, textvariable=self.t_to, width=19)
        e2.pack(side="left", padx=2)
        for e in (e1, e2):
            e.bind("<FocusOut>", lambda ev: self.app.redraw_timeline())
            e.bind("<Return>", lambda ev: self.app.redraw_timeline())
        ttk.Button(r, text="alles", command=self.clear_range).pack(side="left", padx=2)
        ttk.Label(r, textvariable=self.info, foreground="#444").pack(side="left", padx=8)
        f.columnconfigure(1, weight=1)
        return f

    # -- actions -----------------------------------------------------------
    def browse(self):
        p = filedialog.askopenfilename(
            title=f"Quelle {self.label}: CCU-Historian-Datenbank wählen",
            filetypes=[("H2 Datenbank oder Backup", "*.mv.db *.h2.db *.zip"), ("H2 Datenbank", "*.mv.db *.h2.db"),
                       ("Backup (ZIP)", "*.zip"), ("Alle Dateien", "*.*")],
            initialdir=self.app.settings.get("last_dir") or os.path.expanduser("~"))
        if not p:
            return
        self.app.settings["last_dir"] = os.path.dirname(p)
        self.path.set(p)
        self.inspect_file()

    def inspect_file(self):
        if self.path.get().lower().endswith(".zip"):
            return self._inspect_zip()
        try:
            info = E.inspect_db_file(self.path.get())
        except Exception as e:  # noqa: BLE001
            self.info.set(f"Fehler: {e}")
            return None
        txt = f"{info.format_text}, {info.size / 1e6:.1f} MB"
        if info.lock_file:
            txt += "  ⚠ Lock-Datei vorhanden – Historian evtl. aktiv!"
        self.info.set(txt)
        best = self.app.jars.best_for_format(info.fmt, info.kind)
        if best:
            self.jar.set(best)
        elif not self.jar.get():
            self.app.log(f"{self.label}: kein passendes H2-JAR für {info.format_text} vorhanden – "
                         f"bitte über 'H2 laden…' herunterladen oder JAR hinzufügen.")
        return info

    def _inspect_zip(self):
        try:
            zi = E.inspect_zip(self.path.get())
        except Exception as e:  # noqa: BLE001
            self.info.set(f"Fehler: {e}")
            return None
        self.info.set(zi.text())
        jars = self.app.jars
        if zi.kind == "script":
            if zi.script_version is None:
                best = next((d for d, p in jars.paths.items()
                             if (jars.versions.get(p) or "").startswith("1.4.200")), None) or jars.newest()
            else:
                need = E._version_tuple(zi.script_version)
                ok = [d for d, p in jars.paths.items() if E._version_tuple(jars.versions.get(p)) >= need]
                best = min(ok, key=lambda d: E._version_tuple(jars.versions.get(jars.paths[d]))) if ok else None
                if best is None:
                    self.app.log(f"{self.label}: Skript stammt von H2 {zi.script_version} – bitte JAR >= "
                                 f"{zi.script_version} über 'H2 laden…' holen.")
            if best:
                self.jar.set(best)
        return zi

    def clear_range(self):
        self.t_from.set("")
        self.t_to.set("")
        self.app.redraw_timeline()

    def _jar_path(self) -> str:
        p = self.app.jars.paths.get(self.jar.get())
        if not p:
            raise ValueError(f"Quelle {self.label}: kein H2-JAR gewählt")
        return p

    def open(self, recovered_path: Optional[str] = None, original: Optional[str] = None):
        if not self.path.get():
            messagebox.showwarning("Quelle", f"Quelle {self.label}: bitte zuerst eine Datei wählen.")
            return
        if self.inspect_file() is None and not recovered_path:
            return
        try:
            jar = self._jar_path()
        except ValueError as e:
            messagebox.showwarning("JAR", str(e))
            return
        self.close()
        workdir = self.app.workdir()
        path = recovered_path or self.path.get()
        user, pw = self.user.get(), self.password.get()
        w = self.app.worker

        def task():
            if recovered_path:
                db = E.Database(self.label, path, jar, user, pw, workdir, copy=False)
                db.recovered_from = original
                db.set_origin(original)
                db.open(w.progress)
            else:
                db = E.open_source(self.label, path, jar, user, pw, workdir, w.progress, w.cancel)
            try:
                db.scan(w.progress, w.cancel)
            except BaseException:
                db.close()
                raise
            return db

        def done(db):
            self.db = db
            self.selected = {n for n, t in db.tables.items() if t.is_data}
            self.daily.clear()
            mn, mx, total = db.overall_range()
            nerr = sum(1 for t in db.tables.values() if t.error)
            ndata = sum(1 for t in db.tables.values() if t.is_data)
            txt = (f"H2 {db.driver.version} | {E.ms_to_str(mn)} … {E.ms_to_str(mx)} | "
                   f"{ndata} Datenpunkte, {total:,} Werte".replace(",", "."))
            if nerr:
                txt += f" | ⚠ {nerr} Tabellen mit Fehlern"
            if recovered_path:
                txt += " | (gerettete Kopie)"
            elif getattr(db, "zip_info", None) is not None:
                txt += " | (aus ZIP-Backup)"
            self.info.set(txt)
            self.app.log(f"{self.label} geöffnet: {txt}")
            if getattr(db, "analysis", None) is not None and db.analysis.rollback_suspected and not recovered_path:
                if messagebox.askyesno(
                        "Verdacht auf Rollback",
                        f"Quelle {self.label}: {db.analysis.text()}\n\nH2 zeigt dann nur einen alten "
                        "Datenbankstand. Jetzt eine Roh-Extraktion (Carving) starten?"):
                    self.carve()
                    return
            unm = [d for d in db.datapoints if not d.get("_TABLE")]
            if unm:
                self.app.log(f"{self.label}: {len(unm)} DATA_POINTS-Einträge ohne gefundene Datentabelle "
                             f"(Namensschema evtl. abweichend).")
            self.app.refresh_tree()

        def err(e, tb):
            self.app.log(tb)
            self.info.set("Öffnen fehlgeschlagen")
            if isinstance(e, E.Cancelled):
                return
            msg = E.java_error_text(e)
            if recovered_path:
                messagebox.showerror("Fehler", msg[:1000])
                return
            ans = messagebox.askyesnocancel(
                "Öffnen fehlgeschlagen",
                f"Quelle {self.label} konnte nicht geöffnet werden:\n\n{msg[:600]}\n\n"
                "Rettung versuchen? (arbeitet auf einer Kopie, Original bleibt unverändert)\n\n"
                "Ja = Roh-Extraktion (Carving): liest alle noch vorhandenen Datenseiten direkt "
                "(empfohlen für H2 1.4, benötigt h2-1.4.200.jar)\n"
                "Nein = H2 Recover-Werkzeug\nAbbrechen = nichts tun")
            if ans is True:
                self.carve()
            elif ans is False:
                self.recover()

        w.submit(task, done, err, f"{self.label}: öffne {path}")

    def recover(self):
        if not self.path.get():
            return
        if self.path.get().lower().endswith(".zip"):
            messagebox.showinfo("ZIP-Backup", "ZIP-Backups werden mit 'Öffnen' direkt eingespielt; "
                                              "Recover/Carving gilt nur für .mv.db-Dateien.")
            return
        try:
            jar = self._jar_path()
        except ValueError as e:
            messagebox.showwarning("JAR", str(e))
            return
        self.close()
        workdir = self.app.workdir()
        path, user, pw = self.path.get(), self.user.get(), self.password.get()
        w = self.app.worker

        def task():
            return E.recover_database(path, jar, workdir, user, pw, w.progress)

        def done(newpath):
            self.app.log(f"{self.label}: Recover erzeugte {newpath}")
            self.open(recovered_path=newpath, original=path)

        def err(e, tb):
            self.app.log(tb)
            messagebox.showerror("Recover fehlgeschlagen", E.java_error_text(e)[:1000])

        w.submit(task, done, err, f"{self.label}: Recover")

    def deep_check(self):
        if self.db is None:
            messagebox.showinfo("Prüfen", f"Quelle {self.label} bitte zuerst öffnen.")
            return
        db = self.db
        w = self.app.worker
        try:
            min_skip = int(float(self.app.min_skip.get()) * 1000)
        except ValueError:
            min_skip = 600_000

        def task():
            return db.deep_check(w.progress, w.cancel, min_skip)

        def done(res):
            bad = {k: v for k, v in res.items() if v[1] or db.tables[k].error}
            total = sum(v[0] for v in res.values())
            self.app.log(f"{self.label}: Tiefenprüfung – {total:,} Werte gelesen, ".replace(",", ".")
                         + f"{len(bad)} von {len(res)} Tabellen mit unlesbaren Bereichen")
            for k, (n, lost) in bad.items():
                self.app.log(f"   {k}: {db.tables[k].error}")
            self.app.refresh_tree()
            messagebox.showinfo("Tiefenprüfung", f"Quelle {self.label}: {len(bad)} von {len(res)} Tabellen "
                                                 f"mit unlesbaren Bereichen (rot markiert, Details im Protokoll).")

        w.submit(task, done, lambda e, tb: self.app.log(tb), f"{self.label}: Tiefenprüfung")

    def carve(self):
        if not self.path.get():
            return
        if self.path.get().lower().endswith(".zip"):
            messagebox.showinfo("ZIP-Backup", "ZIP-Backups werden mit 'Öffnen' direkt eingespielt; "
                                              "Recover/Carving gilt nur für .mv.db-Dateien.")
            return
        jar = next((p for d, p in self.app.jars.paths.items()
                    if (self.app.jars.versions.get(p) or "").startswith("1.4.200")), None)
        if jar is None:
            messagebox.showwarning("Carving", "Für die Roh-Extraktion wird h2-1.4.200.jar benötigt "
                                              "(Button 'H2 laden…').")
            return
        self.close()
        workdir = self.app.workdir()
        path, user, pw = self.path.get(), self.user.get(), self.password.get()
        w = self.app.worker

        def task():
            from . import carve as C
            return C.carve_database(path, jar, workdir, user, pw, w.progress, w.cancel)

        def done(res):
            out, rep = res
            self.app.log(rep.text())
            self.jar.set(self.app.jars.display_for(jar))
            self.open(recovered_path=out, original=path)

        def err(e, tb):
            self.app.log(tb)
            messagebox.showerror("Carving fehlgeschlagen", E.java_error_text(e)[:1000])

        w.submit(task, done, err, f"{self.label}: Roh-Extraktion (Carving)")

    def close(self):
        if self.db is not None:
            db = self.db
            self.db = None
            self.app.worker.submit(db.close)
            self.app.refresh_tree()

    def range_ms(self) -> tuple[Optional[int], Optional[int]]:
        return E.str_to_ms(self.t_from.get()), E.str_to_ms(self.t_to.get())


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title(f"CCU-Historian Datenbank-Rettung {__version__}")
        root.geometry("1400x900")
        self.settings = load_settings()
        self.jars = JarRegistry(self.settings.get("jars", []))
        self.worker = Worker(root)
        self.worker.on_progress = self._on_progress
        self.worker.on_busy = self._on_busy
        self.slots: list[SourceSlot] = []
        self.filter = tk.StringVar()
        self.status = tk.StringVar(value="Bereit")
        self.out_path = tk.StringVar(value=self.settings.get("out_path", ""))
        self.target_jar = tk.StringVar()
        self.template = tk.StringVar(value="A")
        self.dedup = tk.StringVar(value="timestamp")
        self.min_skip = tk.StringVar(value="600")
        self.compact = tk.BooleanVar(value=True)
        self.keep_work = tk.BooleanVar(value=False)
        self.workdir_var = tk.StringVar(value=self.settings.get(
            "workdir", os.path.join(os.path.expanduser("~"), "historian_repair_work")))
        self.jvm_var = tk.StringVar(value=self.settings.get("jvm", ""))
        self.sel_slot = tk.StringVar(value="A")
        self.current_table: Optional[str] = None
        self._build()
        self.add_slot()
        self.add_slot()
        root.protocol("WM_DELETE_WINDOW", self.on_close)

    # -- layout ------------------------------------------------------------
    def _build(self):
        top = ttk.Frame(self.root, padding=4)
        top.pack(fill="x")
        ttk.Label(top, text="Arbeitsverzeichnis (Kopien):").pack(side="left")
        ttk.Entry(top, textvariable=self.workdir_var, width=45).pack(side="left", padx=2)
        ttk.Button(top, text="…", width=3, command=self._browse_workdir).pack(side="left")
        ttk.Label(top, text="  Java (optional, sonst automatisch):").pack(side="left")
        ttk.Entry(top, textvariable=self.jvm_var, width=30).pack(side="left", padx=2)
        ttk.Button(top, text="JAR hinzufügen…", command=self._add_jar).pack(side="left", padx=(10, 2))
        ttk.Button(top, text="H2 laden…", command=self._download_jar).pack(side="left", padx=2)
        ttk.Button(top, text="+ Quelle", command=self.add_slot).pack(side="right")

        self.slot_area = ttk.LabelFrame(self.root, text="Quell-Datenbanken (Reihenfolge = Priorität, "
                                                        "oben = höchste)", padding=4)
        self.slot_area.pack(fill="x", padx=4)

        pw = ttk.PanedWindow(self.root, orient="vertical")
        pw.pack(fill="both", expand=True, padx=4, pady=4)
        nb = ttk.Notebook(pw)
        self.nb = nb
        pw.add(nb, weight=4)

        # -- tab datapoints
        t1 = ttk.Frame(nb, padding=4)
        nb.add(t1, text="Datenpunkte")
        bar = ttk.Frame(t1)
        bar.pack(fill="x")
        ttk.Label(bar, text="Filter:").pack(side="left")
        fe = ttk.Entry(bar, textvariable=self.filter, width=30)
        fe.pack(side="left", padx=2)
        fe.bind("<KeyRelease>", lambda e: self.refresh_tree())
        ttk.Label(bar, text="   Auswahl für Quelle").pack(side="left")
        self.sel_cb = ttk.Combobox(bar, textvariable=self.sel_slot, width=4, state="readonly")
        self.sel_cb.pack(side="left", padx=2)
        ttk.Button(bar, text="alle (gefiltert) an", command=lambda: self._select_all(True)).pack(side="left")
        ttk.Button(bar, text="alle (gefiltert) aus", command=lambda: self._select_all(False)).pack(side="left")
        ttk.Button(bar, text="Zeitachse zeigen", command=self._show_coverage).pack(side="left", padx=10)
        ttk.Label(bar, text="Klick auf ☑/☐ schaltet einzeln um, Doppelklick zeigt Abdeckung",
                  foreground="#666").pack(side="right")
        tf = ttk.Frame(t1)
        tf.pack(fill="both", expand=True, pady=(4, 0))
        self.tree = ttk.Treeview(tf, show="headings", selectmode="browse")
        vs = ttk.Scrollbar(tf, orient="vertical", command=self.tree.yview)
        hs = ttk.Scrollbar(tf, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vs.grid(row=0, column=1, sticky="ns")
        hs.grid(row=1, column=0, sticky="ew")
        tf.rowconfigure(0, weight=1)
        tf.columnconfigure(0, weight=1)
        self.tree.tag_configure("err", background="#ffd6d6")
        self.tree.tag_configure("single", background="#fff6d0")
        self.tree.bind("<Button-1>", self._tree_click)
        self.tree.bind("<Double-1>", lambda e: self._show_coverage())

        # -- tab timeline
        t2 = ttk.Frame(nb, padding=4)
        nb.add(t2, text="Zeitachse")
        self.tl_title = tk.StringVar(value="Übersicht aller Quellen. Ziehen mit der Maus in einer Zeile "
                                           "setzt den Übernahme-Zeitraum dieser Quelle.")
        ttk.Label(t2, textvariable=self.tl_title).pack(anchor="w")
        self.canvas = tk.Canvas(t2, background="white", height=300)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda e: self.redraw_timeline())
        self.canvas.bind("<ButtonPress-1>", self._tl_press)
        self.canvas.bind("<B1-Motion>", self._tl_drag)
        self.canvas.bind("<ButtonRelease-1>", self._tl_release)
        self._tl_rows: list[tuple[SourceSlot, int, int]] = []
        self._tl_axis: Optional[tuple[int, int, int, int]] = None
        self._drag: Optional[tuple] = None

        # -- tab log
        t3 = ttk.Frame(nb, padding=4)
        nb.add(t3, text="Protokoll")
        self.logbox = ScrolledText(t3, height=10, font=("Courier", 9))
        self.logbox.pack(fill="both", expand=True)

        # -- target
        tg = ttk.LabelFrame(pw, text="Neue Datenbank (Ziel)", padding=6)
        pw.add(tg, weight=0)
        ttk.Label(tg, text="Ausgabedatei:").grid(row=0, column=0, sticky="w")
        ttk.Entry(tg, textvariable=self.out_path, width=70).grid(row=0, column=1, columnspan=3, sticky="ew")
        ttk.Button(tg, text="…", width=3, command=self._browse_out).grid(row=0, column=4, sticky="w")
        ttk.Label(tg, text="H2-Version Ziel:").grid(row=1, column=0, sticky="w")
        self.tjar_cb = ttk.Combobox(tg, textvariable=self.target_jar, width=40, state="readonly")
        self.tjar_cb.grid(row=1, column=1, sticky="w")
        ttk.Label(tg, text="Schema/Meta-Tabellen aus Quelle:").grid(row=1, column=2, sticky="e")
        self.tmpl_cb = ttk.Combobox(tg, textvariable=self.template, width=4, state="readonly")
        self.tmpl_cb.grid(row=1, column=3, sticky="w")
        ttk.Label(tg, text="Überlappung:").grid(row=2, column=0, sticky="nw")
        df = ttk.Frame(tg)
        df.grid(row=2, column=1, columnspan=3, sticky="w")
        ttk.Radiobutton(df, text="Zeitstempel-genau: Werte niedriger priorisierter Quellen nur, wenn der "
                                 "Zeitstempel noch fehlt (füllt Lücken)", value="timestamp",
                        variable=self.dedup).pack(anchor="w")
        ttk.Radiobutton(df, text="Zeitraum-Priorität: innerhalb des von einer höheren Quelle gelieferten "
                                 "Zeitraums (erster … letzter Wert) nichts aus niedrigeren Quellen",
                        value="range", variable=self.dedup).pack(anchor="w")
        of = ttk.Frame(tg)
        of.grid(row=3, column=1, columnspan=3, sticky="w", pady=(4, 0))
        ttk.Label(of, text="Unlesbare Bereiche aufgeben ab [s]:").pack(side="left")
        ttk.Entry(of, textvariable=self.min_skip, width=7).pack(side="left", padx=(2, 12))
        ttk.Checkbutton(of, text="Ziel kompaktieren", variable=self.compact).pack(side="left")
        ttk.Checkbutton(of, text="Arbeitsdateien behalten", variable=self.keep_work).pack(side="left", padx=8)
        btn = ttk.Frame(tg)
        btn.grid(row=0, column=5, rowspan=4, sticky="ne", padx=(12, 0))
        self.b_preview = ttk.Button(btn, text="Vorschau", command=self.preview)
        self.b_preview.pack(fill="x")
        self.b_run = ttk.Button(btn, text="Neue Datenbank erstellen", command=self.run_merge)
        self.b_run.pack(fill="x", pady=4)
        self.b_cancel = ttk.Button(btn, text="Abbrechen", command=self.worker.cancel.set, state="disabled")
        self.b_cancel.pack(fill="x")
        tg.columnconfigure(1, weight=1)

        sb = ttk.Frame(self.root, padding=(4, 2))
        sb.pack(fill="x")
        self.pbar = ttk.Progressbar(sb, length=250, mode="determinate", maximum=1.0)
        self.pbar.pack(side="right")
        ttk.Label(sb, textvariable=self.status).pack(side="left")
        self._refresh_jar_lists()

    # -- helpers -----------------------------------------------------------
    def log(self, msg: str):
        self.logbox.insert("end", msg.rstrip() + "\n")
        self.logbox.see("end")

    def _on_progress(self, msg: str, frac: Optional[float]):
        self.status.set(msg)
        if frac is not None:
            self.pbar.configure(mode="determinate")
            self.pbar["value"] = frac
        if not msg.startswith("[") and "analysiere" not in msg:
            self.log(msg)

    def _on_busy(self, busy: bool):
        self.b_cancel.configure(state="normal" if busy else "disabled")
        self.b_run.configure(state="disabled" if busy else "normal")
        self.b_preview.configure(state="disabled" if busy else "normal")
        if busy:
            self.pbar.configure(mode="indeterminate")
            self.pbar.start(15)
        else:
            self.pbar.stop()
            self.pbar.configure(mode="determinate")
            self.status.set("Bereit")

    def workdir(self) -> str:
        wd = self.workdir_var.get().strip() or os.path.join(os.path.expanduser("~"), "historian_repair_work")
        os.makedirs(wd, exist_ok=True)
        self._ensure_jvm()
        return wd

    def _ensure_jvm(self):
        import jpype
        first = not jpype.isJVMStarted()
        try:
            j = E.start_jvm(self.jvm_var.get().strip() or None)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Java", str(e))
            raise
        if first and j is not None:
            self.log(f"Verwende {j.text()}")

    def _browse_workdir(self):
        d = filedialog.askdirectory(initialdir=self.workdir_var.get())
        if d:
            self.workdir_var.set(d)

    def _browse_out(self):
        p = filedialog.asksaveasfilename(title="Neue Datenbank speichern als", defaultextension=".mv.db",
                                         initialfile="history.mv.db",
                                         filetypes=[("H2 Datenbank", "*.mv.db")])
        if p:
            if not p.endswith(".mv.db"):
                p = p.rsplit(".", 1)[0] + ".mv.db" if p.endswith(".db") else p + ".mv.db"
            self.out_path.set(p)

    def _refresh_jar_lists(self):
        vals = list(self.jars.paths)
        for s in self.slots:
            s.jar_cb.configure(values=vals)
        self.tjar_cb.configure(values=vals)
        if not self.target_jar.get() and vals:
            self.target_jar.set(self.jars.newest() or vals[-1])

    def _add_jar(self):
        p = filedialog.askopenfilename(title="H2-JAR (oder ccu-historian.jar) wählen",
                                       filetypes=[("Java-Archiv", "*.jar")])
        if not p:
            return
        d = self.jars.add(p)
        if not d:
            messagebox.showerror("JAR", "Die Datei enthält keinen H2-Treiber (org.h2.Driver).")
            return
        self.settings["jars"] = self.jars.extra
        self._refresh_jar_lists()
        self.log(f"JAR hinzugefügt: {d}")

    def _download_jar(self):
        win = tk.Toplevel(self.root)
        win.title("H2 von Maven Central laden")
        ttk.Label(win, text="Version wählen (Download von repo1.maven.org):", padding=8).pack()
        v = tk.StringVar(value=E.H2_DOWNLOAD_VERSIONS[0])
        for ver in E.H2_DOWNLOAD_VERSIONS:
            fmt = E.FORMAT_TEXT.get(E.format_for_version(ver), "")
            ttk.Radiobutton(win, text=f"{ver}   – öffnet/schreibt {fmt}", value=ver, variable=v).pack(
                anchor="w", padx=12)

        def go():
            ver = v.get()
            win.destroy()

            def task():
                self.worker.progress(f"Lade h2-{ver}.jar ...")
                return E.download_h2(ver, JAR_DIR)

            def done(p):
                self.jars.refresh()
                self._refresh_jar_lists()
                self.log(f"Heruntergeladen: {p}")
                for s in self.slots:
                    if s.path.get() and not s.jar.get():
                        s.inspect_file()

            self.worker.submit(task, done, lambda e, tb: messagebox.showerror("Download", str(e)))
        ttk.Button(win, text="Herunterladen", command=go).pack(pady=8)

    # -- slots -------------------------------------------------------------
    def add_slot(self):
        used = {s.label for s in self.slots}
        label = next(c for c in string.ascii_uppercase if c not in used)
        s = SourceSlot(self, label)
        s.build(self.slot_area)
        self.slots.append(s)
        self._layout_slots()

    def remove_slot(self, s: SourceSlot):
        if len(self.slots) <= 1:
            return
        s.close()
        s.frame.destroy()
        self.slots.remove(s)
        self._layout_slots()
        self.refresh_tree()

    def move_slot(self, s: SourceSlot, d: int):
        i = self.slots.index(s)
        j = i + d
        if 0 <= j < len(self.slots):
            self.slots[i], self.slots[j] = self.slots[j], self.slots[i]
            self._layout_slots()
            self.refresh_tree()

    def _layout_slots(self):
        for s in self.slots:
            s.frame.pack_forget()
        for s in self.slots:
            s.frame.pack(fill="x", pady=2)
        labels = [s.label for s in self.slots]
        self.sel_cb.configure(values=labels)
        self.tmpl_cb.configure(values=labels)
        if self.sel_slot.get() not in labels:
            self.sel_slot.set(labels[0])
        if self.template.get() not in labels:
            self.template.set(labels[0])
        self._refresh_jar_lists()
        self.redraw_timeline()

    def open_slots(self) -> list[SourceSlot]:
        return [s for s in self.slots if s.db is not None]

    # -- datapoint table ---------------------------------------------------
    def refresh_tree(self):
        slots = self.open_slots()
        cols = ["name", "table"]
        for s in slots:
            cols += [f"{s.label}_sel", f"{s.label}_from", f"{s.label}_to", f"{s.label}_n"]
        self.tree.configure(columns=cols)
        self.tree.heading("name", text="Name (DATA_POINTS)")
        self.tree.column("name", width=220, stretch=False)
        self.tree.heading("table", text="Tabelle")
        self.tree.column("table", width=320, stretch=False)
        for s in slots:
            self.tree.heading(f"{s.label}_sel", text=s.label)
            self.tree.column(f"{s.label}_sel", width=30, anchor="center", stretch=False)
            self.tree.heading(f"{s.label}_from", text=f"{s.label}: erster Wert")
            self.tree.column(f"{s.label}_from", width=135, stretch=False)
            self.tree.heading(f"{s.label}_to", text=f"{s.label}: letzter Wert")
            self.tree.column(f"{s.label}_to", width=135, stretch=False)
            self.tree.heading(f"{s.label}_n", text=f"{s.label}: Anzahl")
            self.tree.column(f"{s.label}_n", width=80, anchor="e", stretch=False)
        self.tree.delete(*self.tree.get_children())
        names: dict[str, str] = {}
        for s in slots:
            for n, t in s.db.tables.items():
                if t.is_data and (n not in names or not names[n]):
                    names[n] = t.datapoint.get("DISPLAY_NAME") if t.datapoint else ""
        flt = self.filter.get().strip().lower()
        for n in sorted(names, key=lambda x: ((names[x] or "~").lower(), x)):
            if flt and flt not in n.lower() and flt not in str(names[n] or "").lower():
                continue
            vals = [names[n] or "", n]
            err = False
            present = 0
            for s in slots:
                t = s.db.tables.get(n)
                if t is None or not t.is_data:
                    vals += ["", "", "", "–"]
                    continue
                present += 1
                err = err or bool(t.error)
                vals += [CHECK_ON if n in s.selected else CHECK_OFF, E.ms_to_str(t.min_ms),
                         E.ms_to_str(t.max_ms), "FEHLER" if t.error and t.count is None else str(t.count)]
            tags = ("err",) if err else (("single",) if len(slots) > 1 and present == 1 else ())
            self.tree.insert("", "end", iid=n, values=vals, tags=tags)
        self.redraw_timeline()

    def _tree_click(self, ev):
        if self.tree.identify_region(ev.x, ev.y) != "cell":
            return
        col = self.tree.identify_column(ev.x)
        row = self.tree.identify_row(ev.y)
        if not row:
            return
        cols = self.tree["columns"]
        idx = int(col[1:]) - 1
        if idx < 0 or idx >= len(cols):
            return
        cname = cols[idx]
        if not cname.endswith("_sel"):
            return
        lab = cname[:-4]
        s = next((x for x in self.slots if x.label == lab and x.db), None)
        if s is None or row not in s.db.tables:
            return
        if row in s.selected:
            s.selected.discard(row)
        else:
            s.selected.add(row)
        self.tree.set(row, cname, CHECK_ON if row in s.selected else CHECK_OFF)
        return "break"

    def _select_all(self, on: bool):
        s = next((x for x in self.slots if x.label == self.sel_slot.get() and x.db), None)
        if s is None:
            return
        for iid in self.tree.get_children():
            if iid in s.db.tables:
                (s.selected.add if on else s.selected.discard)(iid)
                self.tree.set(iid, f"{s.label}_sel", CHECK_ON if on else CHECK_OFF)

    # -- timeline ----------------------------------------------------------
    def _show_coverage(self):
        sel = self.tree.selection()
        if not sel:
            self.current_table = None
            self.nb.select(1)
            self.redraw_timeline()
            return
        table = sel[0]
        self.current_table = table
        slots = [s for s in self.open_slots() if table in s.db.tables and table not in s.daily]

        def task():
            res = {}
            for s in slots:
                try:
                    res[s.label] = s.db.daily_counts(table, self.worker.cancel)
                except Exception as e:  # noqa: BLE001
                    s.db.reconnect()
                    res[s.label] = E.java_error_text(e)
            return res

        def done(res):
            for s in self.slots:
                if s.label in res:
                    s.daily[table] = res[s.label]
            self.nb.select(1)
            self.redraw_timeline()

        self.worker.submit(task, done, lambda e, tb: self.log(tb), f"Lade Tagesabdeckung {table}")

    def _time_bounds(self) -> Optional[tuple[int, int]]:
        lo, hi = None, None
        for s in self.open_slots():
            mn, mx, _ = s.db.overall_range()
            if mn is not None:
                lo = mn if lo is None else min(lo, mn)
                hi = mx if hi is None else max(hi, mx)
        if lo is None:
            return None
        pad = max(E.DAY_MS, (hi - lo) // 50)
        return lo - pad, hi + pad

    def redraw_timeline(self):
        c = getattr(self, "canvas", None)
        if c is None:
            return
        c.delete("all")
        self._tl_rows = []
        b = self._time_bounds()
        W = max(400, c.winfo_width())
        if b is None:
            c.create_text(20, 20, anchor="nw", text="Noch keine Quelle geöffnet.", fill="#666")
            return
        lo, hi = b
        x0, x1 = 90, W - 20
        self._tl_axis = (lo, hi, x0, x1)

        def X(ms):
            return x0 + (ms - lo) / (hi - lo) * (x1 - x0)

        # axis with month/year ticks
        import datetime as dt
        y_axis = 22
        c.create_line(x0, y_axis, x1, y_axis, fill="#888")
        d0 = E.EPOCH + dt.timedelta(milliseconds=lo)
        span_days = (hi - lo) / E.DAY_MS
        step_m = 1 if span_days < 500 else (3 if span_days < 1500 else 12)
        m = dt.datetime(d0.year, d0.month, 1)
        while True:
            ms = int((m - E.EPOCH).total_seconds() * 1000)
            if ms > hi:
                break
            if ms >= lo:
                x = X(ms)
                c.create_line(x, y_axis - 4, x, y_axis + 4, fill="#888")
                c.create_text(x + 2, y_axis - 6, anchor="sw", font=("TkDefaultFont", 8),
                              text=m.strftime("%Y" if step_m == 12 else "%m/%y"))
                c.create_line(x, y_axis + 4, x, 2000, fill="#eee")
            mm = m.month - 1 + step_m
            m = dt.datetime(m.year + mm // 12, mm % 12 + 1, 1)

        y = 40
        c.create_text(5, y, anchor="nw", text="Gesamt (erster…letzter Wert aller Datenpunkte):",
                      font=("TkDefaultFont", 9, "bold"))
        y += 18
        for s in self.slots:
            if s.db is None:
                continue
            mn, mx, _ = s.db.overall_range()
            row_top = y
            c.create_text(5, y + 9, anchor="w", text=f"Quelle {s.label}", fill=s.color)
            if mn is not None:
                c.create_rectangle(X(mn), y + 3, X(mx), y + 15, fill="#ddd", outline=s.color)
            try:
                f, t = s.range_ms()
            except ValueError:
                f, t = None, None
            if s.active.get() and mn is not None:
                a = max(mn, f) if f is not None else mn
                z = min(mx, t) if t is not None else mx
                if a <= z:
                    c.create_rectangle(X(a), y + 3, X(z), y + 15, fill=s.color, outline=s.color,
                                       stipple="gray50")
            self._tl_rows.append((s, row_top, row_top + 18))
            y += 22
        c.create_text(5, y + 2, anchor="nw", fill="#666", font=("TkDefaultFont", 8),
                      text="grau = vorhandene Daten, farbig = zu übernehmender Zeitraum; "
                           "Maus ziehen = Zeitraum setzen, Rechtsklick in Zeile = Zeitraum zurücksetzen")
        y += 24
        c.bind("<Button-3>", self._tl_reset)

        # datapoint coverage
        tbl = self.current_table
        if tbl:
            c.create_text(5, y, anchor="nw", font=("TkDefaultFont", 9, "bold"),
                          text=f"Datenpunkt {tbl}: Werte pro Tag")
            y += 18
            for s in self.slots:
                if s.db is None or tbl not in s.db.tables:
                    continue
                d = s.daily.get(tbl)
                h = 50
                c.create_text(5, y + h / 2, anchor="w", text=f"Quelle {s.label}", fill=s.color)
                c.create_line(x0, y + h, x1, y + h, fill="#ccc")
                if isinstance(d, str):
                    c.create_text(x0 + 4, y + h / 2, anchor="w", text=f"Fehler: {d[:150]}", fill="red")
                elif d:
                    mxv = max(n for _, n in d) or 1
                    wd = max(1.0, (x1 - x0) * E.DAY_MS / (hi - lo))
                    nbad = 0
                    for ms, n in d:
                        xa = X(ms)
                        if n < 0:
                            nbad += 1
                            c.create_rectangle(xa, y, xa + max(wd, 2), y + h, fill="#ff4040", width=0)
                        else:
                            c.create_rectangle(xa, y + h - h * n / mxv, xa + wd, y + h, fill=s.color, width=0)
                    c.create_text(x1, y, anchor="ne", font=("TkDefaultFont", 8),
                                  text=f"max {mxv}/Tag, {len(d) - nbad} Tage mit Daten"
                                       + (f", {nbad} Tage UNLESBAR (rot)" if nbad else ""))
                elif d is None:
                    c.create_text(x0 + 4, y + h / 2, anchor="w", text="(Doppelklick in der Tabelle lädt Daten)",
                                  fill="#888")
                y += h + 8
        c.configure(scrollregion=(0, 0, W, y + 20))

    def _tl_slot_at(self, ev) -> Optional[SourceSlot]:
        for s, a, b in self._tl_rows:
            if a <= ev.y <= b:
                return s
        return None

    def _tl_ms(self, x) -> int:
        lo, hi, x0, x1 = self._tl_axis
        x = min(max(x, x0), x1)
        return int(lo + (x - x0) / (x1 - x0) * (hi - lo))

    def _tl_press(self, ev):
        s = self._tl_slot_at(ev)
        if s is None or self._tl_axis is None:
            self._drag = None
            return
        self._drag = (s, ev.x)

    def _tl_drag(self, ev):
        if not self._drag:
            return
        s, xs = self._drag
        a, b = sorted((self._tl_ms(xs), self._tl_ms(ev.x)))
        s.t_from.set(E.ms_to_str(a - a % 60_000))
        s.t_to.set(E.ms_to_str(b - b % 60_000 + 59_999))
        self.redraw_timeline()

    def _tl_release(self, ev):
        if self._drag and abs(ev.x - self._drag[1]) < 3:
            pass
        self._drag = None

    def _tl_reset(self, ev):
        s = self._tl_slot_at(ev)
        if s:
            s.clear_range()

    # -- merge -------------------------------------------------------------
    def _collect(self) -> Optional[tuple[list[E.SourceSpec], E.MergeOptions]]:
        active = [s for s in self.slots if s.active.get()]
        not_open = [s.label for s in active if s.db is None]
        if not_open:
            messagebox.showwarning("Quellen", f"Quelle(n) {', '.join(not_open)} sind aktiv, aber nicht "
                                              f"geöffnet. Bitte öffnen oder Haken 'übernehmen' entfernen.")
            return None
        if not active:
            messagebox.showwarning("Quellen", "Keine aktive Quelle.")
            return None
        specs = []
        for s in active:
            try:
                f, t = s.range_ms()
            except ValueError as e:
                messagebox.showerror("Zeitraum", f"Quelle {s.label}: {e}")
                return None
            if f is not None and t is not None and f > t:
                messagebox.showerror("Zeitraum", f"Quelle {s.label}: 'von' liegt nach 'bis'.")
                return None
            specs.append(E.SourceSpec(s.db, set(s.selected), f, t))
        out = self.out_path.get().strip()
        if not out:
            messagebox.showwarning("Ziel", "Bitte eine Ausgabedatei angeben.")
            return None
        tjar = self.jars.paths.get(self.target_jar.get())
        if not tjar:
            messagebox.showwarning("Ziel", "Bitte die H2-Version der neuen Datenbank wählen.")
            return None
        labels = [s.label for s in active]
        if self.template.get() not in labels:
            messagebox.showwarning("Ziel", "Die Schema-Vorlage muss eine aktive Quelle sein.")
            return None
        try:
            min_skip = int(float(self.min_skip.get()) * 1000)
        except ValueError:
            messagebox.showerror("Option", "Ungültiger Wert für 'Unlesbare Bereiche aufgeben ab'.")
            return None
        opts = E.MergeOptions(output_path=out, target_jar=tjar, template=labels.index(self.template.get()),
                              dedup=self.dedup.get(), min_skip_ms=min_skip, compact=self.compact.get(),
                              keep_workdir=self.keep_work.get(),
                              workdir=os.path.join(self.workdir(), "merge"))
        return specs, opts

    def preview(self):
        c = self._collect()
        if not c:
            return
        specs, opts = c
        w = self.worker

        def task():
            lines = []
            for sp in specs:
                n_tab, n_rows, n_err = 0, 0, 0
                for name in sorted(sp.tables or []):
                    t = sp.db.tables.get(name)
                    if t is None or not t.is_data:
                        continue
                    n_tab += 1
                    w.progress(f"Vorschau {sp.db.label}: {name}")
                    if self.worker.cancel.is_set():
                        raise E.Cancelled()
                    try:
                        if sp.t_from is None and sp.t_to is None:
                            n_rows += t.count or 0
                        else:
                            n_rows += sp.db.count_range(name, sp.t_from, sp.t_to)
                    except Exception:  # noqa: BLE001
                        sp.db.reconnect()
                        n_err += 1
                rng = f"{E.ms_to_str(sp.t_from) or 'Anfang'} … {E.ms_to_str(sp.t_to) or 'Ende'}"
                lines.append(f"Quelle {sp.db.label}: {n_tab} Datenpunkte, Zeitraum {rng}, "
                             f"bis zu {n_rows:,} Werte".replace(",", ".")
                             + (f", {n_err} Tabellen nicht zählbar" if n_err else ""))
            lines.append("")
            lines.append("Priorität: " + " > ".join(sp.db.label for sp in specs)
                         + ("  (Zeitstempel-genau)" if opts.dedup == "timestamp" else "  (Zeitraum-Priorität)"))
            lines.append(f"Ziel: {opts.output_path}  mit {self.target_jar.get()}")
            lines.append("Duplikate werden erst beim Erstellen ermittelt.")
            return "\n".join(lines)

        w.submit(task, lambda txt: (self.log(txt), messagebox.showinfo("Vorschau", txt)),
                 lambda e, tb: self.log(tb), "Vorschau")

    def run_merge(self):
        c = self._collect()
        if not c:
            return
        specs, opts = c
        out = opts.output_path if opts.output_path.endswith(".mv.db") else opts.output_path + ".mv.db"
        if os.path.exists(out):
            if not messagebox.askyesno("Überschreiben?", f"{out}\nexistiert bereits. Überschreiben?\n"
                                                         "(Quell-Datenbanken werden nie überschrieben.)"):
                return
            opts.overwrite = True
        self.settings["out_path"] = self.out_path.get()
        save_settings(self._settings_dict())
        w = self.worker

        def task():
            job = E.MergeJob(specs, opts, w.progress, w.cancel)
            rep = job.run()
            rpath = rep.output_path[:-6] + "_repair_report.txt"
            E.write_report(rep, rpath)
            return rep, rpath

        def done(res):
            rep, rpath = res
            self.log(rep.text())
            self.log(f"Bericht gespeichert: {rpath}")
            self.nb.select(2)
            if rep.ok:
                messagebox.showinfo("Fertig", f"Neue Datenbank erstellt:\n{rep.output_path}\n\nBericht: {rpath}")
            else:
                messagebox.showwarning("Fertig mit Warnungen",
                                       f"Neue Datenbank erstellt:\n{rep.output_path}\n\nEs gab Lesefehler, "
                                       f"Abweichungen oder Warnungen – siehe Protokoll/Bericht:\n{rpath}")

        def err(e, tb):
            self.log(tb)
            if isinstance(e, E.Cancelled):
                messagebox.showinfo("Abgebrochen", "Vorgang abgebrochen. Es wurde keine Ausgabedatei erzeugt.")
            else:
                messagebox.showerror("Fehler", E.java_error_text(e)[:1500])

        w.submit(task, done, err, "Erstelle neue Datenbank")

    # -- close -------------------------------------------------------------
    def _settings_dict(self) -> dict:
        s = dict(self.settings)
        s.update({"jars": self.jars.extra, "workdir": self.workdir_var.get(), "jvm": self.jvm_var.get(),
                  "out_path": self.out_path.get()})
        return s

    def on_close(self):
        if self.worker.busy and not messagebox.askyesno("Beenden", "Ein Vorgang läuft noch. Trotzdem beenden?"):
            return
        save_settings(self._settings_dict())
        for s in self.slots:
            if s.db:
                try:
                    s.db.close()
                except Exception:  # noqa: BLE001
                    pass
        self.root.destroy()


def main() -> int:
    root = tk.Tk()
    try:
        ttk.Style().theme_use("clam" if os.name != "nt" else "vista")
    except tk.TclError:
        pass
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    main()
