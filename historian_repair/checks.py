"""Static check of a (merged) database against the requirements of CCU-Historian.

Reference: github.com/mdzio/ccu-historian, ccu-historian/src/mdz/ccuhistorian/Database.groovy
(v4.1.3) and external-libs/ (bundled H2 version). The database is opened as a
read-only copy; nothing is changed.
"""
from __future__ import annotations

import threading
from typing import Optional

from . import h2engine as E

OK, INFO, WARN, ERROR = "OK", "INFO", "WARNUNG", "FEHLER"
SYMBOL = {OK: "✔", INFO: "ℹ", WARN: "⚠", ERROR: "✘"}

# columns of DATA_POINTS as created by CCU-Historian 4.1.3 (Database.prepareDatabase)
DP_COLUMNS_413 = ["DP_ID", "TABLE_NAME", "STATE", "INTERFACE", "ADDRESS", "IDENTIFIER", "PREPROC_TYPE",
                  "PREPROC_PARAM", "DISPLAY_NAME", "ROOM", "FUNCTION", "COMMENT", "CUSTOM", "PARAM_SET",
                  "TAB_ORDER", "MAXIMUM", "UNIT", "MINIMUM", "CONTROL", "OPERATIONS", "FLAGS", "TYPE",
                  "DEFAULT_VALUE"]
FORMAT_HISTORIAN = {1: "CCU-Historian 2.2 – 2.9 (H2 1.4.199)",
                    2: "CCU-Historian 3.0 – 4.2.x (H2 2.1.212/2.1.214)",
                    3: "keiner CCU-Historian-Version (H2 2.2 oder neuer)"}


class CheckResult:
    def __init__(self, path: str):
        self.path = path
        self.items: list[tuple[str, str]] = []

    def add(self, level: str, text: str) -> None:
        self.items.append((level, text))

    @property
    def errors(self) -> int:
        return sum(1 for lv, _ in self.items if lv == ERROR)

    @property
    def warnings(self) -> int:
        return sum(1 for lv, _ in self.items if lv == WARN)

    def text(self) -> str:
        head = (f"Zielprüfung für CCU-Historian 4.1.3: {self.path}\n"
                f"Ergebnis: {'GEEIGNET' if not self.errors else 'NICHT GEEIGNET'} "
                f"({self.errors} Fehler, {self.warnings} Warnungen)")
        return head + "\n" + "\n".join(f"  {SYMBOL[lv]} {lv:8s} {t}" for lv, t in self.items)


def _q1(conn, sql, params=()):
    r = E.query_all(conn, sql, params)
    return E.py(r[0][0]) if r else None


def check_historian_target(path: str, jar: str, user: str = E.HISTORIAN_DEFAULT_USER,
                           password: str = E.HISTORIAN_DEFAULT_PASSWORD, workdir: str = ".",
                           progress: E.ProgressCb = E._noop_progress,
                           cancel: Optional[threading.Event] = None, reuse: bool = True) -> CheckResult:
    res = CheckResult(path)
    info = E.inspect_db_file(path)
    want = E.HISTORIAN_H2[0][1]
    # 1. file format
    if info.kind != "mv":
        res.add(ERROR, "PageStore-Datei (.h2.db) – CCU-Historian 3.x/4.x benötigt eine .mv.db (Format 2)")
        return res
    lvl = OK if info.fmt == 2 else ERROR
    res.add(lvl, f"Dateiformat {info.fmt} ({info.format_text}) – öffnebar von {FORMAT_HISTORIAN.get(info.fmt, '?')}")
    if info.fmt != 2:
        res.add(ERROR, f"Historian 4.1.3 (H2 {want}) kann diese Datei nicht öffnen. Neu erzeugen mit "
                       f"Ziel-JAR h2-{want}.jar.")
        return res
    drv_ver = E.jar_version(jar)
    if E.format_for_version(drv_ver) != info.fmt:
        res.add(ERROR, f"Zum Prüfen wurde H2 {drv_ver} gewählt, passt nicht zu Format {info.fmt}")
        return res
    # 2. login
    db = E.Database("Ziel", path, jar, user, password, workdir, reuse=reuse)
    try:
        db.open(progress)
    except Exception as e:  # noqa: BLE001
        msg = E.java_error_text(e)
        if "28000" in msg or "Wrong user name or password" in msg:
            res.add(ERROR, f"Anmeldung mit Benutzer '{user}' und dem eingetragenen Passwort fehlgeschlagen – "
                           "der Historian meldet sich mit database.user/database.password aus der "
                           "ccu-historian.config an (Standard sa / ccu-historian)")
        else:
            res.add(ERROR, f"Datenbank lässt sich nicht öffnen: {msg[:200]}")
        return res
    res.add(OK, f"Anmeldung als '{user}' erfolgreich")
    try:
        _check_open_db(db, res, progress, cancel)
    except E.Cancelled:
        raise
    except Exception as e:  # noqa: BLE001
        res.add(ERROR, f"Prüfung abgebrochen: {E.java_error_text(e)[:250]}")
    finally:
        db.close()
    return res


def _check_open_db(db, res: CheckResult, progress, cancel) -> None:
    if True:
        c = db.conn
        db.scan(progress, cancel)
        tables = db.tables
        # 3. CONFIG
        if "CONFIG" not in tables:
            res.add(INFO, "Tabelle CONFIG fehlt – der Historian legt sie beim Start an und behandelt die DB "
                          "als neu (DATA_POINTS bleiben erhalten)")
        elif not {"NAME", "VALUE"} <= {x.upper() for x in tables["CONFIG"].column_names}:
            res.add(ERROR, "Tabelle CONFIG hat nicht die Historian-Struktur (NAME, VALUE) – der Historian "
                           "scheitert beim Start an SELECT \"VALUE\" FROM CONFIG")
        else:
            v = _q1(c, 'SELECT "VALUE" FROM CONFIG WHERE NAME=?', ["internal.databaseVersion"])
            if v is None:
                res.add(INFO, "internal.databaseVersion fehlt – Historian setzt beim Start Version 4")
            elif str(v) == "4":
                res.add(OK, "internal.databaseVersion = 4")
            else:
                res.add(INFO, f"internal.databaseVersion = {v} – Historian migriert beim Start auf 4 "
                              "(ALTER TABLE CONFIG …)")
        # 4. DATA_POINTS
        if "DATA_POINTS" not in tables:
            res.add(ERROR, "Tabelle DATA_POINTS fehlt – der Historian würde keine Verläufe anzeigen")
            return
        cols = [x.upper() for x in tables["DATA_POINTS"].column_names]
        missing = [x for x in DP_COLUMNS_413 if x not in cols]
        res.add(ERROR if missing else OK,
                f"DATA_POINTS-Spalten {'fehlen: ' + ', '.join(missing) if missing else 'vollständig (23)'}")
        if "TABLE_NAME" in cols:
            n_null = _q1(c, "SELECT COUNT(*) FROM DATA_POINTS WHERE TABLE_NAME IS NULL")
            if n_null:
                res.add(ERROR, f"{n_null} Datenpunkte ohne TABLE_NAME")
        dup = _q1(c, "SELECT COUNT(*) FROM (SELECT INTERFACE, ADDRESS, IDENTIFIER FROM DATA_POINTS "
                     "GROUP BY INTERFACE, ADDRESS, IDENTIFIER HAVING COUNT(*) > 1)")
        res.add(ERROR if dup else OK, f"{dup} doppelte Datenpunkte (INTERFACE, ADDRESS, IDENTIFIER)" if dup
                else "keine doppelten Datenpunkte")
        n_dp = _q1(c, "SELECT COUNT(*) FROM DATA_POINTS")
        max_id = _q1(c, "SELECT MAX(DP_ID) FROM DATA_POINTS") or 0
        # 5. DP_ID identity -> next generated id must be above the maximum
        try:
            r = E.query_all(c, "SELECT IS_IDENTITY, IDENTITY_GENERATION, IDENTITY_BASE FROM "
                               "INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME='DATA_POINTS' AND COLUMN_NAME='DP_ID'")
            is_id, gen, base = (E.py(x) for x in r[0])
            if str(is_id).upper() != "YES":
                res.add(ERROR, "DP_ID ist keine Identity-Spalte – neue Datenpunkte bekämen keine ID")
            elif base is not None and int(base) <= int(max_id):
                res.add(ERROR, f"nächste DP_ID {base} ≤ vorhandenes Maximum {max_id} – neue Datenpunkte "
                               "würden kollidieren")
            else:
                res.add(OK, f"DP_ID: Identity ({gen}), nächste ID {base} > Maximum {max_id}")
        except Exception as e:  # noqa: BLE001
            res.add(WARN, f"Identity von DP_ID nicht prüfbar: {E.java_error_text(e)[:120]}")
        # 6. data tables
        dps = E.query_all(c, "SELECT TABLE_NAME FROM DATA_POINTS")
        names = [str(E.py(r[0])) for r in dps if r[0] is not None]
        idx = {}
        for r in E.query_all(c, "SELECT TABLE_NAME, INDEX_NAME FROM INFORMATION_SCHEMA.INDEX_COLUMNS "
                                "WHERE TABLE_SCHEMA='PUBLIC' AND COLUMN_NAME='TS' AND ORDINAL_POSITION=1"):
            idx.setdefault(str(E.py(r[0])), set()).add(str(E.py(r[1])))
        no_table, bad_type, no_idx, other_idx, multi_idx = [], [], [], [], []
        for t in names:
            ti = tables.get(t)
            if ti is None:
                no_table.append(t)
                continue
            types = {n.upper(): ty.upper() for n, ty in ti.columns}
            vt = types.get("VALUE", "")
            if t.startswith("C_") and "CHAR" not in vt or t.startswith("D_") and "DOUBLE" not in vt:
                bad_type.append(f"{t} ({vt})")
            ix = idx.get(t, set())
            if not ix:
                no_idx.append(t)
            elif len(ix) > 1:
                multi_idx.append(t)
            elif f"{t}_IDX" not in ix:
                other_idx.append(t)
        res.add(OK if not no_table else ERROR,
                f"{len(names) - len(no_table)} von {len(names)} Datenpunkten haben ihre Datentabelle"
                + (f"; OHNE Tabelle: {', '.join(no_table[:5])}" + (" …" if len(no_table) > 5 else "")
                   + " – der Historian legt Tabellen nur für neue Datenpunkte an, Werte für diese "
                     "Datenpunkte könnten nicht gespeichert werden (mit aktueller Tool-Version neu erzeugen)"
                   if no_table else ""))
        if bad_type:
            res.add(ERROR, f"{len(bad_type)} Tabellen mit falschem VALUE-Typ (C_ = Text, D_ = Zahl): "
                           + ", ".join(bad_type[:5]))
        if no_idx:
            res.add(INFO, f"{len(no_idx)} Tabellen ohne TS-Index – Historian legt <Tabelle>_IDX beim ersten "
                          "Zugriff an (kann bei großen Tabellen dauern)")
        if other_idx:
            res.add(INFO, f"{len(other_idx)} Tabellen mit anders benanntem TS-Index – Historian legt "
                          "zusätzlich <Tabelle>_IDX an (doppelter Index, nur Speicherplatz)")
        if multi_idx:
            res.add(INFO, f"{len(multi_idx)} Tabellen mit mehreren TS-Indizes (nur Speicherplatz)")
        if not (no_idx or other_idx or multi_idx):
            res.add(OK, "TS-Indizes überall als <Tabelle>_IDX vorhanden")
        orphans = [n for n, t in tables.items() if t.is_data and n not in set(names)]
        if orphans:
            res.add(INFO, f"{len(orphans)} Datentabellen ohne Eintrag in DATA_POINTS (z.B. CARVED_*) – "
                          "für den Historian unsichtbar, stören nicht")
        # 7. aliases (FORCE aliases are not listed in INFORMATION_SCHEMA.ROUTINES without the class)
        script = "\n".join(str(E.py(r[0])) for r in E.query_all(c, "SCRIPT NODATA NOPASSWORDS NOSETTINGS")
                           if "ALIAS" in str(E.py(r[0])))
        miss = [a for a in ("TS_TO_UNIX", "UNIX_TO_TS") if f'"{a}"' not in script and f" {a} " not in script]
        res.add(WARN if miss else OK, f"Funktionen fehlen: {', '.join(miss)} (Historian legt sie nur in neuen "
                                      "Datenbanken an; Abfragen, die sie nutzen, schlagen fehl)"
                if miss else "Funktionen TS_TO_UNIX/UNIX_TO_TS vorhanden")
        mn, mx, tot = db.overall_range()
        res.add(INFO, f"{n_dp} Datenpunkte, {E.fmt_n(tot)} Werte, {E.ms_to_str(mn)} … {E.ms_to_str(mx)}")
