"""Command line interface (also used for automated tests)."""
from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import threading
import time

from . import h2engine as E


_last_detail = [0.0]


def _progress(msg: str, frac=None, detail: bool = False) -> None:
    """Milestones are always printed, frequent status updates at most every 5 s."""
    if detail:
        now = time.time()
        if now - _last_detail[0] < 5:
            return
        _last_detail[0] = now
    pct = f"{frac * 100:5.1f}% " if frac is not None else "       "
    print(f"{pct}{msg}", flush=True)


def _split_src(spec: str, default_jar: str | None) -> tuple[str, str]:
    if "@" in spec:
        path, jar = spec.rsplit("@", 1)
    else:
        path, jar = spec, default_jar
    if not jar:
        raise SystemExit(f"Kein H2-JAR für {path} angegeben (--jar oder PFAD@JAR)")
    return path, jar


def cmd_info(a) -> int:
    if a.db.lower().endswith(".zip"):
        print(E.inspect_zip(a.db).text())
        if not a.jar:
            return 0
        wd = a.workdir or os.path.join(os.path.dirname(os.path.abspath(a.db)), "historian_repair_work")
        db = E.open_source("A", a.db, a.jar, a.user, a.password, wd, _progress)
        return _print_db(db, a)
    info = E.inspect_db_file(a.db)
    print(f"Datei:   {info.path}")
    if info.kind == "mv":
        from .carve import analyze_mvstore
        print(f"Struktur: {analyze_mvstore(a.db).text()}")
    print(f"Größe:   {info.size / 1e6:.1f} MB")
    print(f"Format:  {info.format_text}  Header={info.header}")
    if info.lock_file:
        print(f"WARNUNG: Lock-Datei vorhanden ({info.lock_file}) - läuft der Historian noch?")
    if not a.jar:
        return 0
    wd = a.workdir or os.path.join(os.path.dirname(info.path), "historian_repair_work")
    db = E.open_source("A", a.db, a.jar, a.user, a.password, wd, _progress)
    return _print_db(db, a)


def _print_db(db, a) -> int:
    try:
        db.scan(_progress)
        mn, mx, total = db.overall_range()
        print(f"Zeitraum: {E.ms_to_str(mn)} .. {E.ms_to_str(mx)}   Zeilen gesamt: {total}")
        print(f"Tabellen: {len(db.tables)}, Datentabellen: {sum(t.is_data for t in db.tables.values())}, "
              f"DATA_POINTS-Einträge: {len(db.datapoints)}")
        unmapped = [d for d in db.datapoints if not d.get('_TABLE')]
        if unmapped:
            print(f"DATA_POINTS ohne zugeordnete Tabelle: {len(unmapped)}")
        for t in sorted(db.tables.values(), key=lambda t: t.name):
            if not t.is_data:
                print(f"  [Meta] {t.name}  Spalten={t.column_names}")
                continue
            if a.verbose or t.error:
                print(f"  {t.name:60s} {E.ms_to_str(t.min_ms)} .. {E.ms_to_str(t.max_ms)} "
                      f"n={t.count} {('FEHLER: ' + t.error) if t.error else ''}")
    finally:
        db.close()
        print("Original unverändert:", db.original_unchanged())
    return 0


def cmd_recover(a) -> int:
    wd = a.workdir or os.path.join(os.path.dirname(os.path.abspath(a.db)), "historian_repair_work")
    out = E.recover_database(a.db, a.jar, wd, a.user, a.password, _progress)
    print("Wiederhergestellte Datenbank:", out)
    return 0


def cmd_carve(a) -> int:
    from . import carve as C
    wd = a.workdir or os.path.join(os.path.dirname(os.path.abspath(a.db)), "historian_repair_work")
    print(C.analyze_mvstore(a.db).text())
    out, rep = C.carve_database(a.db, a.jar, wd, a.user, a.password, _progress)
    print(rep.text())
    print("Neue Datenbank mit geretteten Daten:", out)
    return 0


def cmd_merge(a) -> int:
    ranges = {}
    for r in a.range or []:
        m = re.match(r"^(\w+)=(.*)\.\.(.*)$", r)
        if not m:
            raise SystemExit(f"--range erwartet LABEL=VON..BIS, nicht {r}")
        ranges[m.group(1)] = (E.str_to_ms(m.group(2)), E.str_to_ms(m.group(3)))
    out = os.path.abspath(a.out)
    wd = a.workdir or os.path.join(os.path.dirname(out), "historian_repair_work")
    labels = [chr(ord("A") + i) for i in range(len(a.src))]
    specs = []
    tables_re = re.compile(a.tables) if a.tables else None
    try:
        for lab, spec in zip(labels, a.src):
            path, jar = _split_src(spec, a.jar)
            db = E.open_source(lab, path, jar, a.user, a.password, wd, _progress)
            db.scan(_progress)
            mn, mx, total = db.overall_range()
            print(f"{lab}: {path}  {E.ms_to_str(mn)} .. {E.ms_to_str(mx)}  Zeilen={total}")
            sel = None
            if tables_re:
                sel = {n for n, t in db.tables.items() if t.is_data and tables_re.search(n)}
            lo, hi = ranges.get(lab, (None, None))
            specs.append(E.SourceSpec(db, sel, lo, hi))
        tjar = a.target_jar or _split_src(a.src[0], a.jar)[1]
        tver = E.jar_version(tjar)
        if tver != E.HISTORIAN_H2[0][1]:
            print(f"WARNUNG: Ziel wird mit H2 {tver} geschrieben. CCU-Historian 3.0.3–4.2.x verwendet "
                  f"H2 {E.HISTORIAN_H2[0][1]} und kann Dateien anderer H2-Versionen nicht öffnen "
                  f"(--target-jar jars/h2-{E.HISTORIAN_H2[0][1]}.jar).")
        opts = E.MergeOptions(output_path=out, target_jar=tjar, user=a.target_user, password=a.target_password,
                              template=a.template, dedup=a.dedup, min_skip_ms=int(a.min_skip * 1000),
                              compact=not a.no_compact, overwrite=a.overwrite, keep_workdir=a.keep_workdir,
                              workdir=wd)
        job = E.MergeJob(specs, opts, _progress, threading.Event())
        rep = job.run()
        print(rep.text())
        rpath = out[:-6] + "_repair_report.txt" if out.endswith(".mv.db") else out + "_repair_report.txt"
        E.write_report(rep, rpath)
        print("Bericht:", rpath)
        return 0 if rep.ok else 2
    finally:
        for s in specs:
            s.db.close()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="historian_repair",
                                description="CCU-Historian H2-Datenbanken analysieren, retten und zusammenführen")
    p.add_argument("--jvm", help="Java: Pfad zur jvm.dll/libjvm.so oder Java-Verzeichnis (optional, sonst automatisch)")
    p.add_argument("--xmx", help="max. Java-Heap, z.B. 2g")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--user", default="sa")
        sp.add_argument("--password", default="")
        sp.add_argument("--workdir")

    sp = sub.add_parser("info", help="Datenbank analysieren (Zeiträume, Tabellen)")
    sp.add_argument("db")
    sp.add_argument("--jar", help="H2-JAR (ohne: nur Dateiheader anzeigen)")
    sp.add_argument("-v", "--verbose", action="store_true")
    common(sp)
    sp.set_defaults(fn=cmd_info)

    sp = sub.add_parser("recover", help="Nicht öffnbare DB mit org.h2.tools.Recover retten")
    sp.add_argument("db")
    sp.add_argument("--jar", required=True)
    common(sp)
    sp.set_defaults(fn=cmd_recover)

    sp = sub.add_parser("carve", help="Roh-Extraktion aller lesbaren Datenseiten (H2 1.4, braucht h2-1.4.200.jar)")
    sp.add_argument("db")
    sp.add_argument("--jar", required=True)
    common(sp)
    sp.set_defaults(fn=cmd_carve)

    sp = sub.add_parser("merge", help="Neue DB aus Quellen erzeugen (Reihenfolge = Priorität)")
    sp.add_argument("--src", action="append", required=True, help="PFAD[@JAR], mehrfach; PFAD darf .mv.db, .h2.db oder .zip (Backup) sein")
    sp.add_argument("--jar", help="Standard-JAR für alle Quellen")
    sp.add_argument("--target-jar", help="H2-JAR der neuen DB (Standard: JAR der ersten Quelle)")
    sp.add_argument("--out", required=True, help="neue .mv.db-Datei")
    sp.add_argument("--range", action="append", help="LABEL=VON..BIS, z.B. B=2024-01-01..2024-03-01")
    sp.add_argument("--tables", help="Regex auf Datentabellennamen (Auswahl)")
    sp.add_argument("--template", type=int, default=0, help="Index der Schema-Vorlage (0=A)")
    sp.add_argument("--dedup", choices=["timestamp", "range"], default="timestamp")
    sp.add_argument("--min-skip", type=float, default=3600, help="kleinster aufgegebener Bereich [s]")
    sp.add_argument("--no-compact", action="store_true")
    sp.add_argument("--overwrite", action="store_true")
    sp.add_argument("--keep-workdir", action="store_true")
    sp.add_argument("--target-user", default=E.HISTORIAN_DEFAULT_USER, help="Benutzer der neuen DB (Standard sa)")
    sp.add_argument("--target-password", default=E.HISTORIAN_DEFAULT_PASSWORD,
                    help="Passwort der neuen DB (Standard ccu-historian, wie database.password im Historian)")
    common(sp)
    sp.set_defaults(fn=cmd_merge)

    a = p.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    try:
        j = E.start_jvm(a.jvm, a.xmx)
    except RuntimeError as e:
        print(f"FEHLER: {e}")
        return 3
    print(f"Verwende {j.text()}")
    t0 = time.time()
    rc = a.fn(a)
    print(f"({time.time() - t0:.1f} s)")
    return rc


if __name__ == "__main__":
    sys.exit(main())
