"""Verified reuse of work data (source copies, ZIP import, deep check, recover, carving).

Every result directory in the work directory gets a ``manifest.json`` with

* kind of the result and CACHE_FORMAT (bumped whenever the processing logic
  changes in a way that changes the result) plus the tool version,
* the source file: path, size, modification time and the full SHA-256
  checksum (computed while the source is copied, i.e. in the same read pass),
* the H2 version used and processing settings ("extra"),
* size and modification time of the result right after it was written.

A result is reused only if checksum, H2 version, settings and CACHE_FORMAT
match and the result file is unchanged. Directories without a manifest
(e.g. from older tool versions) are never reused. Reuse can be switched off
("neu auswerten").
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import shutil
import threading
import time
from typing import Callable, Optional

from . import __version__

# Bump when import/carve/recover/check logic changes the produced result.
# 2 (tool 0.7.0): full SHA-256, DATETIME->TIMESTAMP for 1.x scripts, carving with index linking
CACHE_FORMAT = 2
MANIFEST = "manifest.json"
_CHUNK = 8 * 1024 * 1024


def _noop(msg: str, frac=None, detail: bool = False) -> None:
    pass


def file_sha256(path: str, progress: Callable = _noop, text: str = "Prüfsumme",
                cancel: Optional[threading.Event] = None) -> str:
    """SHA-256 of a whole file (read only), with progress."""
    from .h2engine import fmt_mb, _check_cancel
    total = os.path.getsize(path)
    h = hashlib.sha256()
    done, last = 0, 0.0
    with open(path, "rb") as f:
        while True:
            _check_cancel(cancel)
            b = f.read(_CHUNK)
            if not b:
                break
            h.update(b)
            done += len(b)
            if time.time() - last >= 0.5:
                last = time.time()
                progress(f"{text}: {fmt_mb(done)} von {fmt_mb(total)}", None, True)
    return h.hexdigest()


def source_fingerprint(path: str, sha256: Optional[str] = None, progress: Callable = _noop,
                       cancel: Optional[threading.Event] = None) -> dict:
    st = os.stat(path)
    return {"path": os.path.abspath(path), "size": st.st_size, "mtime_ns": st.st_mtime_ns,
            "sha256": sha256 or file_sha256(path, progress, "Prüfsumme der Quelle", cancel)}


def _result_fp(path: str) -> dict:
    if os.path.isdir(path):
        return {"dir": True}
    st = os.stat(path)
    return {"size": st.st_size, "mtime_ns": st.st_mtime_ns}


def write_manifest(result_dir: str, kind: str, source_fp: dict, h2_version: str, result: str,
                   extra: Optional[dict] = None) -> None:
    m = {"kind": kind, "cache_format": CACHE_FORMAT, "tool_version": __version__,
         "created": time.strftime("%Y-%m-%d %H:%M:%S"), "source": source_fp, "h2_version": h2_version,
         "result": os.path.relpath(result, result_dir), "result_fp": _result_fp(result),
         "extra": extra or {}}
    with open(os.path.join(result_dir, MANIFEST), "w", encoding="utf-8") as f:
        json.dump(m, f, indent=1)


def read_manifest(result_dir: str) -> Optional[dict]:
    try:
        with open(os.path.join(result_dir, MANIFEST), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


class Reuse:
    """Looks up reusable results for one source file; computes its checksum at most once."""

    def __init__(self, workdir: str, source: str, enabled: bool = True, progress: Callable = _noop,
                 cancel: Optional[threading.Event] = None, sha256: Optional[str] = None):
        self.workdir = os.path.abspath(workdir)
        self.source = os.path.abspath(source)
        self.enabled = enabled
        self.progress = progress
        self.cancel = cancel
        self._sha = sha256

    def sha256(self) -> str:
        if self._sha is None:
            self._sha = file_sha256(self.source, self.progress, "Prüfsumme der Quelle", self.cancel)
        return self._sha

    def set_sha256(self, sha: str) -> None:
        self._sha = sha

    def fingerprint(self) -> dict:
        return source_fingerprint(self.source, self.sha256())

    def find(self, kind: str, h2_version: str, extra: Optional[dict] = None) -> tuple[Optional[str], str]:
        """(result_path, explanation); result_path None if nothing reusable."""
        if not self.enabled:
            return None, "Wiederverwendung abgeschaltet (neu auswerten)"
        cands = sorted(glob.glob(os.path.join(self.workdir, f"{kind}_*", MANIFEST)),
                       key=os.path.getmtime, reverse=True)
        size = os.path.getsize(self.source)
        reasons = []
        for mf in cands:
            d = os.path.dirname(mf)
            m = read_manifest(d)
            if not m or m.get("kind") != kind:
                continue
            ms = m.get("source", {})
            if ms.get("size") != size:
                continue                          # different file, not worth hashing
            if m.get("cache_format") != CACHE_FORMAT:
                reasons.append(f"{os.path.basename(d)}: ältere Verarbeitungslogik (Tool {m.get('tool_version')})")
                continue
            if m.get("h2_version") != h2_version:
                reasons.append(f"{os.path.basename(d)}: andere H2-Version ({m.get('h2_version')})")
                continue
            if (m.get("extra") or {}) != (extra or {}):
                reasons.append(f"{os.path.basename(d)}: andere Einstellungen")
                continue
            if ms.get("sha256") != self.sha256():
                reasons.append(f"{os.path.basename(d)}: Prüfsumme der Quelle abweichend")
                continue
            res = os.path.join(d, m.get("result", ""))
            if not os.path.exists(res):
                reasons.append(f"{os.path.basename(d)}: Ergebnis fehlt")
                continue
            if _result_fp(res) != m.get("result_fp"):
                reasons.append(f"{os.path.basename(d)}: Ergebnis wurde seitdem verändert")
                continue
            h2 = f", H2 {h2_version}" if h2_version != "-" else ""
            return res, (f"wiederverwendet: {os.path.basename(d)} vom {m.get('created')} "
                         f"(Tool {m.get('tool_version')}{h2}, SHA-256 der Quelle identisch "
                         f"{self.sha256()[:12]}…)")
        if reasons:
            return None, "nicht wiederverwendbar – " + "; ".join(reasons[:3])
        return None, "keine früheren Arbeitsdaten zu dieser Quelle"

    def store(self, result_dir: str, kind: str, h2_version: str, result: str,
              extra: Optional[dict] = None) -> None:
        write_manifest(result_dir, kind, self.fingerprint(), h2_version, result, extra)


def remove_stale(workdir: str, kind: str, source: str, keep: str) -> None:
    """Delete older result dirs of the same kind for the same source path (content changed)."""
    for mf in glob.glob(os.path.join(os.path.abspath(workdir), f"{kind}_*", MANIFEST)):
        d = os.path.dirname(mf)
        if os.path.abspath(d) == os.path.abspath(keep):
            continue
        m = read_manifest(d)
        if m and m.get("source", {}).get("path") == os.path.abspath(source):
            shutil.rmtree(d, ignore_errors=True)


def legacy_dirs(workdir: str) -> list[str]:
    """Work directories without manifest (older tool versions) - never reused, safe to delete."""
    out = []
    if not os.path.isdir(workdir):
        return out
    for name in sorted(os.listdir(workdir)):
        d = os.path.join(workdir, name)
        if not os.path.isdir(d):
            continue
        if name.startswith(("src_", "zip_", "carve_", "recover_", "copy_", "check_")) and \
                not os.path.exists(os.path.join(d, MANIFEST)):
            out.append(d)
    merge = os.path.join(workdir, "merge")
    if os.path.isdir(merge):
        out += [os.path.join(merge, n) for n in sorted(os.listdir(merge)) if n.startswith("target_")]
    return out
