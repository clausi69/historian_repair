"""Compatibility check against CCU-Historian 4.1.3 (github.com/mdzio/ccu-historian, tag v4.1.3).

The SQL statements below are taken verbatim from
ccu-historian/src/mdz/ccuhistorian/Database.groovy (v4.1.3) and are executed
with the H2 version bundled with the Historian (2.1.214) and the same JDBC URL
options. This does not run the Historian itself, it replays its database
access pattern against a database produced by historian_repair.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from historian_repair import h2engine as E  # noqa: E402

URL_OPTS = ";MODE=PostgreSQL;DB_CLOSE_ON_EXIT=FALSE;BUILTIN_ALIAS_OVERRIDE=TRUE"   # Database.getUrl()

NEW_DB_SQL = [  # Database.prepareDatabase(), branch "new database detected"
    '''CREATE TABLE IF NOT EXISTS DATA_POINTS (
        DP_ID INT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        TABLE_NAME VARCHAR NOT NULL,
        STATE INT,
        INTERFACE VARCHAR NOT NULL, ADDRESS VARCHAR NOT NULL,
        IDENTIFIER VARCHAR NOT NULL,
        PREPROC_TYPE INT, PREPROC_PARAM DOUBLE,
        DISPLAY_NAME VARCHAR, ROOM VARCHAR, FUNCTION VARCHAR, COMMENT VARCHAR,
        CUSTOM VARCHAR DEFAULT '{}',
        PARAM_SET VARCHAR, TAB_ORDER INT,
        MAXIMUM DOUBLE, UNIT VARCHAR,
        MINIMUM DOUBLE, CONTROL VARCHAR,
        OPERATIONS INT, FLAGS INT,
        TYPE VARCHAR, DEFAULT_VALUE DOUBLE
    ); CREATE UNIQUE INDEX IF NOT EXISTS DATA_POINTS_IDX
        ON DATA_POINTS (INTERFACE, ADDRESS, IDENTIFIER)''',
    'CREATE ALIAS IF NOT EXISTS TS_TO_UNIX DETERMINISTIC FOR "mdz.ccuhistorian.DatabaseExtensions.TS_TO_UNIX"',
    'CREATE ALIAS IF NOT EXISTS UNIX_TO_TS DETERMINISTIC FOR "mdz.ccuhistorian.DatabaseExtensions.UNIX_TO_TS"',
]


def _get_config(c, name):
    r = E.query_all(c, 'SELECT "VALUE" FROM CONFIG WHERE NAME=?', [name])
    return str(r[0][0]) if r else None


def _set_config(c, name, value):
    ps = c.prepareStatement('UPDATE CONFIG SET "VALUE"=? WHERE NAME=?')
    ps.setObject(1, value)
    ps.setObject(2, name)
    if ps.executeUpdate() == 0:
        ps2 = c.prepareStatement('INSERT INTO CONFIG VALUES (?, ?)')
        ps2.setObject(1, name)
        ps2.setObject(2, value)
        ps2.executeUpdate()


def prepare_database(c) -> str:
    """Database.prepareDatabase() of v4.1.3. Returns what happened."""
    E.execute(c, 'CREATE TABLE IF NOT EXISTS CONFIG (NAME VARCHAR(128) NOT NULL, "VALUE" VARCHAR(65536))')
    v = _get_config(c, 'internal.databaseVersion')
    if v is None:
        for sql in NEW_DB_SQL:
            E.execute(c, sql)
        _set_config(c, 'internal.databaseVersion', '4')
        return "neue Datenbank erkannt (Version 4 gesetzt)"
    if int(v) < 4:
        E.execute(c, '''ALTER TABLE CONFIG ALTER COLUMN "VALUE" VARCHAR(65536)''')
        _set_config(c, 'internal.databaseVersion', '4')
        return f"migriert von Version {v} auf 4"
    return f"Version {v}, keine Migration"


def create_data_point_table(c, table: str, string: bool):
    """Database.createDataPointTable()"""
    vt = "VARCHAR" if string else "DOUBLE"
    E.execute(c, f'''CREATE TABLE IF NOT EXISTS {table} (TS DATETIME, "VALUE" {vt}, STATE INT);
        CREATE INDEX IF NOT EXISTS {table}_IDX ON {table} (TS)''')


def create_data_point_entry(c, table, iface, addr, ident) -> int:
    """Database.createDataPointEntry(): INSERT without DP_ID, returns generated key."""
    ps = c.prepareStatement('''INSERT INTO DATA_POINTS (
            TABLE_NAME, STATE,
            INTERFACE, ADDRESS, IDENTIFIER,
            PREPROC_TYPE, PREPROC_PARAM,
            DISPLAY_NAME, ROOM, FUNCTION, COMMENT, CUSTOM,
            PARAM_SET, TAB_ORDER, MAXIMUM, UNIT, MINIMUM, CONTROL,
            OPERATIONS, FLAGS, TYPE, DEFAULT_VALUE
        ) VALUES (?, 0, ?, ?, ?, NULL, NULL, 'Test', NULL, NULL, NULL, '{}', 'VALUES', 1, 100.0, '', 0.0,
                  NULL, 5, 1, 'FLOAT', 0.0)''', 1)   # RETURN_GENERATED_KEYS
    for i, v in enumerate([table, iface, addr, ident], 1):
        ps.setObject(i, v)
    ps.executeUpdate()
    rs = ps.getGeneratedKeys()
    rs.next()
    return int(rs.getLong(1))


def historian_startup_check(db_base: str, jar: str, user="sa", password="ccu-historian",
                            full_read: bool = True, stub_jar: str | None = None) -> list[str]:
    """Replays Historian 4.1.3 accesses; returns a list of problems (empty = OK).

    stub_jar: jar with mdz.ccuhistorian.DatabaseExtensions (tests/stub), as the
    Historian has this class on its class path.
    """
    problems: list[str] = []
    drv = E.H2Driver.get(jar, [stub_jar] if stub_jar else None)
    c = drv.connect(f"jdbc:h2:file:{db_base}{URL_OPTS}", user, password)
    try:
        info = prepare_database(c)
        print("  prepareDatabase:", info)
        dps = E.query_all(c, 'SELECT * FROM DATA_POINTS ORDER BY INTERFACE, DISPLAY_NAME, ADDRESS, IDENTIFIER')
        cols = [str(x) for x in E.query_all(c, "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                                               "WHERE TABLE_NAME='DATA_POINTS' ORDER BY ORDINAL_POSITION")]
        cols = [x.strip("(),' ") for x in cols]
        ti = cols.index("TABLE_NAME")
        print(f"  DATA_POINTS: {len(dps)} Einträge")
        n_val = 0
        for row in dps:
            t = str(row[ti])
            try:
                create_data_point_table(c, t, t.startswith("C_"))
                E.query_all(c, f"SELECT MIN(TS) FROM {t}")
                E.query_all(c, f'SELECT TS, "VALUE", STATE FROM {t} WHERE TS=(SELECT MAX(TS) FROM {t})')
                if full_read:
                    rows = E.query_all(c, f'SELECT TS, "VALUE", STATE FROM {t} WHERE TS>=? AND TS<? ORDER BY TS',
                                       [E.make_ts(0), E.make_ts(4102444800000)])
                    n_val += len(rows)
                nidx = E.query_all(c, "SELECT COUNT(DISTINCT INDEX_NAME) FROM INFORMATION_SCHEMA.INDEX_COLUMNS "
                                      "WHERE TABLE_NAME=? AND COLUMN_NAME='TS'", [t])[0][0]
                if int(E.py(nidx)) != 1:
                    problems.append(f"{t}: {E.py(nidx)} Indizes auf TS (erwartet 1)")
            except Exception as e:  # noqa: BLE001
                problems.append(f"{t}: {E.java_error_text(e)[:200]}")
        print(f"  alle Verläufe gelesen: {E.fmt_n(n_val)} Werte")
        # new data point + value, as the Historian does when a new device appears
        max_id = int(E.py(E.query_all(c, "SELECT MAX(DP_ID) FROM DATA_POINTS")[0][0]) or 0)
        create_data_point_table(c, "D_TEST_COMPAT_1_LEVEL", False)
        new_id = create_data_point_entry(c, "D_TEST_COMPAT_1_LEVEL", "TEST", "COMPAT:1", "LEVEL")
        if new_id <= max_id:
            problems.append(f"neue DP_ID {new_id} <= vorhandenes Maximum {max_id}")
        print(f"  neuer Datenpunkt angelegt: DP_ID {new_id} (bisheriges Maximum {max_id})")
        ps = c.prepareStatement('INSERT INTO D_TEST_COMPAT_1_LEVEL (TS, "VALUE", STATE) VALUES (?, ?, ?)')
        ps.setObject(1, E.make_ts(1700000000000))
        ps.setObject(2, 1.5)
        ps.setObject(3, 0)
        ps.executeUpdate()
        if dps:
            t = str(dps[0][ti])
            ps = c.prepareStatement(f'INSERT INTO {t} (TS, "VALUE", STATE) VALUES (?, ?, ?)')
            ps.setObject(1, E.make_ts(1800000000000))
            ps.setObject(2, "x" if t.startswith("C_") else 2.5)
            ps.setObject(3, 0)
            ps.executeUpdate()
        for a in ("TS_TO_UNIX", "UNIX_TO_TS"):
            if not E.query_all(c, "SELECT ROUTINE_NAME FROM INFORMATION_SCHEMA.ROUTINES WHERE ROUTINE_NAME=?", [a]):
                problems.append(f"Alias {a} fehlt")
        ver = _get_config(c, 'internal.databaseVersion')
        if ver != '4':
            problems.append(f"internal.databaseVersion={ver}")
        c.rollback() if not c.getAutoCommit() else None
    finally:
        c.close()
    return problems
