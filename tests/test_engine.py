"""End-to-end tests with synthetic databases.

Requires JPype, a Java runtime and H2 jars in ./jars (h2-1.4.200.jar and
h2-2.3.232.jar); tests are skipped otherwise.
Run: python -m pytest -q tests
"""
import os
import shutil
import sys
import threading

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from historian_repair import h2engine as E  # noqa: E402

J14 = os.path.join(ROOT, "jars", "h2-1.4.200.jar")
J23 = os.path.join(ROOT, "jars", "h2-2.3.232.jar")
pytestmark = pytest.mark.skipif(not (os.path.exists(J14) and os.path.exists(J23)),
                                reason="H2 jars not present in ./jars")


@pytest.fixture(scope="module")
def dbs(tmp_path_factory):
    import make_testdb as M
    E.start_jvm()
    d = tmp_path_factory.mktemp("db")
    a = str(d / "a" / "history")
    b = str(d / "b" / "history")
    os.makedirs(os.path.dirname(a))
    os.makedirs(os.path.dirname(b))
    M.create(a, J14, "2024-01-01", "2024-03-31 23:59:59", 600)
    M.create(b, J23, "2024-03-01", "2024-05-31", 600, gap=("2024-04-01", "2024-04-05"),
             extra_dp=("HmIP-RF", "000B:1", "LEVEL", "Rollladen", False))
    # damaged copy of A
    bad = str(d / "bad" / "history")
    os.makedirs(os.path.dirname(bad))
    shutil.copy(a + ".mv.db", bad + ".mv.db")
    sz = os.path.getsize(bad + ".mv.db")
    with open(bad + ".mv.db", "r+b") as f:
        f.seek(int(sz * 0.35))
        f.write(b"\xAA" * 3000)
    return d, a + ".mv.db", b + ".mv.db", bad + ".mv.db"


def _open(label, path, jar, wd):
    db = E.Database(label, path, jar, workdir=wd)
    db.open()
    db.scan()
    return db


def _merge(d, srcs, out, dedup="timestamp", tjar=J23):
    specs = [E.SourceSpec(db, None, lo, hi) for db, lo, hi in srcs]
    opts = E.MergeOptions(output_path=str(d / out), target_jar=tjar, dedup=dedup, min_skip_ms=600_000,
                          workdir=str(d / "work"), overwrite=True)
    return E.MergeJob(specs, opts, cancel=threading.Event()).run()


def test_format_detection(dbs):
    _, a, b, _ = dbs
    assert E.inspect_db_file(a).fmt == 1
    assert E.inspect_db_file(b).fmt == 3


def test_merge_timestamp(dbs):
    d, a, b, _ = dbs
    fa, fb = E.file_fingerprint(a), E.file_fingerprint(b)
    A = _open("A", a, J14, str(d / "work"))
    B = _open("B", b, J23, str(d / "work"))
    rep = _merge(d, [(A, None, None), (B, None, None)], "m1.mv.db")
    A.close()
    B.close()
    assert rep.ok, rep.text()
    assert E.file_fingerprint(a) == fa and E.file_fingerprint(b) == fb
    st = {(s.table, s.source): s for s in rep.stats}
    t = "D_BIDCOS_RF_LEQ0000001_1_TEMPERATURE"
    # March overlaps: 31 days * 144 values (plus boundary value on 2024-03-31 00:00..)
    assert st[(t, "A")].inserted == st[(t, "A")].read
    assert st[(t, "B")].dup > 0
    assert st[(t, "B")].inserted + st[(t, "B")].dup == st[(t, "B")].read
    assert ("D_HMIP_RF_000B_1_LEVEL", "B") in st
    for exp, got in rep.verify.values():
        assert exp == got


def test_merge_range_and_window(dbs):
    d, a, b, _ = dbs
    A = _open("A", a, J14, str(d / "work"))
    B = _open("B", b, J23, str(d / "work"))
    lo = E.str_to_ms("2024-02-01")
    hi = E.str_to_ms("2024-02-15 23:59:59")
    rep = _merge(d, [(A, lo, hi), (B, None, None)], "m2.mv.db", dedup="range")
    A.close()
    B.close()
    st = {(s.table, s.source): s for s in rep.stats}
    t = "D_BIDCOS_RF_LEQ0000001_1_TEMPERATURE"
    assert st[(t, "A")].min_ms >= lo and st[(t, "A")].max_ms <= hi
    assert st[(t, "A")].inserted == 15 * 144
    assert st[(t, "B")].range_skip == 0  # B starts after A's window
    assert rep.ok


def test_damaged_source_filled_from_other(dbs):
    d, a, b, bad = dbs
    X = _open("X", bad, J14, str(d / "work"))
    A = _open("A", a, J14, str(d / "work"))
    rep = _merge(d, [(X, None, None), (A, None, None)], "m3.mv.db", tjar=J14)
    X.close()
    A.close()
    lost = [s for s in rep.stats if s.lost]
    assert lost, "expected unreadable ranges in damaged copy"
    # all values must be present in the result thanks to A
    for s in rep.stats:
        if s.source == "A":
            assert s.inserted + s.dup == s.read
    tot = {}
    for s in rep.stats:
        tot[s.table] = tot.get(s.table, 0) + s.inserted
    A2 = _open("A2", a, J14, str(d / "work"))
    for name, t in A2.tables.items():
        if t.is_data:
            assert tot[name] == t.count, name
    A2.close()


def test_carve_intact_db(dbs):
    from historian_repair import carve as C
    d, a, _, _ = dbs
    out, rep = C.carve_database(a, J14 if E.jar_version(J14).startswith("1.4.200") else J14, str(d / "work"))
    A = _open("A", a, J14, str(d / "work"))
    X = _open("X", out, J14, str(d / "work"))
    for name, t in A.tables.items():
        if t.is_data:
            assert X.tables[name].count == t.count, name
            assert (X.tables[name].min_ms, X.tables[name].max_ms) == (t.min_ms, t.max_ms)
    assert len(X.datapoints) == len(A.datapoints)
    A.close()
    X.close()


def test_rollback_detection(dbs):
    from historian_repair import carve as C
    _, a, _, _ = dbs
    ana = C.analyze_mvstore(a)
    assert not ana.rollback_suspected
    assert ana.chunks


J25 = os.path.join(ROOT, "jars", "h2-2.5.252.jar")


def _script_zip(dbfile, jar, zpath):
    c = E.H2Driver.get(jar).connect(f"jdbc:h2:file:{dbfile[:-6]};IFEXISTS=TRUE;ACCESS_MODE_DATA=r", "sa", "")
    E.execute(c, f"SCRIPT TO '{zpath}' COMPRESSION ZIP")
    c.close()


@pytest.mark.skipif(not os.path.exists(J25), reason="h2-2.5.252.jar missing")
def test_zip_sources(dbs):
    import zipfile
    d, a, b, _ = dbs
    za = str(d / "backup_a_14.zip")
    zb = str(d / "backup_b_23.zip")
    zdb = str(d / "backup_db.zip")
    shutil.copy(a, str(d / "tmp_a.mv.db"))
    _script_zip(str(d / "tmp_a.mv.db"), J14, za)            # 1.4 script (no version line)
    _script_zip(b, J23, zb)                                   # 2.3 script
    with zipfile.ZipFile(zdb, "w") as z:                      # zip with database file
        z.write(a, "history.mv.db")
    assert E.inspect_zip(za).script_version is None
    assert E.inspect_zip(zb).script_version == "2.3.232"
    assert E.inspect_zip(zdb).kind == "db"
    A = _open("A", a, J14, str(d / "work"))
    ref = {n: (t.count, t.min_ms, t.max_ms) for n, t in A.tables.items() if t.is_data}
    A.close()
    for zpath, jar in ((za, J14), (za, J25), (zdb, J14)):
        fp = E.file_fingerprint(zpath)
        X = E.open_source("Z", zpath, jar, workdir=str(d / "work"))
        X.scan()
        got = {n: (t.count, t.min_ms, t.max_ms) for n, t in X.tables.items() if t.is_data}
        assert got == ref, (zpath, jar)
        assert len(X.datapoints) == 5
        X.close()
        assert E.file_fingerprint(zpath) == fp
    with pytest.raises(RuntimeError):
        E.open_source("Z", zb, J14, workdir=str(d / "work"))
    Y = E.open_source("Y", zb, J25, workdir=str(d / "work"))
    Y.scan()
    # merge zip sources into a new 2.5 database
    X = E.open_source("X", za, J25, workdir=str(d / "work"))
    X.scan()
    rep = E.MergeJob([E.SourceSpec(X), E.SourceSpec(Y)],
                     E.MergeOptions(output_path=str(d / "zipmerge.mv.db"), target_jar=J25,
                                    workdir=str(d / "work"), overwrite=True)).run()
    X.close()
    Y.close()
    assert rep.ok, rep.text()
    assert all(rep.sources_unchanged.values())


J21 = os.path.join(ROOT, "jars", "h2-2.1.214.jar")


@pytest.mark.skipif(not os.path.exists(J21), reason="h2-2.1.214.jar missing")
def test_ccu_historian_413_compat(dbs, tmp_path):
    """Merge into H2 2.1.214 and replay the DB accesses of CCU-Historian 4.1.3."""
    import subprocess
    import historian_compat as H
    d, a, b, _ = dbs
    stub = None
    try:  # compile the DatabaseExtensions stub (needs a JDK)
        cls = tmp_path / "stubcls"
        src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stub", "mdz", "ccuhistorian",
                           "DatabaseExtensions.java")
        subprocess.run(["javac", "--release", "11", "-d", str(cls), src], check=True, capture_output=True)
        stub = str(cls)
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("javac not available")
    # database as created by CCU-Historian 4.1.3 (DP_ID GENERATED ALWAYS, version 4)
    base = str(tmp_path / "hist4" / "history")
    os.makedirs(os.path.dirname(base))
    c = E.H2Driver.get(J21, [stub]).connect(f"jdbc:h2:file:{base}{H.URL_OPTS}", "sa", "ccu-historian")
    H.prepare_database(c)
    H.create_data_point_table(c, "D_BIDCOS_RF_LEQ0000001_1_TEMPERATURE", False)
    H.create_data_point_entry(c, "D_BIDCOS_RF_LEQ0000001_1_TEMPERATURE", "BidCos-RF", "LEQ0000001:1", "TEMPERATURE")
    c.close()
    X = E.open_source("A", base + ".mv.db", J21, "sa", "ccu-historian", str(tmp_path / "w"))
    A = E.open_source("B", a, J14, "sa", "", str(tmp_path / "w"))
    X.scan()
    A.scan()
    out = str(tmp_path / "out" / "history.mv.db")
    rep = E.MergeJob([E.SourceSpec(X), E.SourceSpec(A)],
                     E.MergeOptions(output_path=out, target_jar=J21, workdir=str(tmp_path / "m"),
                                    user="sa", password="ccu-historian")).run()
    X.close()
    A.close()
    assert rep.ok, rep.summary()
    assert E.inspect_db_file(out).fmt == 2
    problems = H.historian_startup_check(out[:-6], J21, stub_jar=stub)
    assert not problems, problems


def test_reuse_workdata(dbs, tmp_path):
    from historian_repair import cache, carve as C
    d, a, _, _ = dbs
    wd = str(tmp_path / "w")
    src = str(tmp_path / "src" / "history.mv.db")
    os.makedirs(os.path.dirname(src))
    shutil.copy(a, src)
    # old work data without manifest (older tool version) is ignored
    os.makedirs(os.path.join(wd, "copy_old_x"))
    shutil.copy(a, os.path.join(wd, "copy_old_x", "history.mv.db"))
    X = E.open_source("A", src, J14, "sa", "", wd)
    assert X.reuse_info == "" and X.source_sha
    first_copy = X.copy_base
    X.scan()
    res1 = X.deep_check(min_skip_ms=600_000)
    X.close()
    Y = E.open_source("A", src, J14, "sa", "", wd)
    assert Y.reuse_info.startswith("wiederverwendet") and Y.copy_base == first_copy
    assert Y.source_sha == X.source_sha
    Y.scan()
    res2 = Y.deep_check(min_skip_ms=600_000)
    assert Y.reuse_info.startswith("wiederverwendet") and res2 == res1
    Y.close()
    Z = E.open_source("A", src, J14, "sa", "", wd, reuse=False)       # "neu auswerten"
    assert Z.reuse_info == "" and Z.copy_base != first_copy
    Z.close()
    # same size, one byte different -> checksum differs -> not reused
    r = cache.Reuse(wd, src)
    found, _ = r.find("copy", "-", {"kind": "mv"})
    assert found
    with open(src, "r+b") as f:
        f.seek(os.path.getsize(src) // 2)
        b = f.read(1)
        f.seek(-1, 1)
        f.write(bytes([b[0] ^ 0xFF]))
    found, why = cache.Reuse(wd, src).find("copy", "-", {"kind": "mv"})
    assert found is None and "Prüfsumme" in why
    # carving result reused
    o1, r1 = C.carve_database(a, J14, wd)
    o2, r2 = C.carve_database(a, J14, wd)
    assert o1 == o2 and r2.reused_info.startswith("wiederverwendet")


@pytest.mark.skipif(not os.path.exists(J21), reason="h2-2.1.214.jar missing")
def test_check_target(dbs, tmp_path):
    from historian_repair import checks
    d, a, _, _ = dbs
    A = E.open_source("A", a, J14, "sa", "", str(tmp_path / "w"))
    A.scan()
    out = str(tmp_path / "out" / "history.mv.db")
    rep = E.MergeJob([E.SourceSpec(A)], E.MergeOptions(output_path=out, target_jar=J21,
                     workdir=str(tmp_path / "m"), user="sa", password="ccu-historian")).run()
    A.close()
    assert rep.ok
    res = checks.check_historian_target(out, J21, "sa", "ccu-historian", str(tmp_path / "w"))
    assert res.errors == 0, res.text()
    bad = checks.check_historian_target(out, J21, "sa", "falsch", str(tmp_path / "w"))
    assert bad.errors == 1
    if os.path.exists(J25):
        out25 = str(tmp_path / "out25" / "history.mv.db")
        A = E.open_source("A", a, J14, "sa", "", str(tmp_path / "w"))
        A.scan()
        E.MergeJob([E.SourceSpec(A)], E.MergeOptions(output_path=out25, target_jar=J25,
                   workdir=str(tmp_path / "m"))).run()
        A.close()
        r25 = checks.check_historian_target(out25, J25, "sa", "", str(tmp_path / "w"))
        assert r25.errors >= 1 and "Dateiformat 3" in r25.text()
