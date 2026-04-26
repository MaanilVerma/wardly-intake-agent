"""Disk persistence for generated briefs.

Atomic writes (temp file + rename) so a partial write can never leave a
half-written brief on disk for a clinician to read. The directory is
gitignored — these files contain simulated PHI.
"""

from __future__ import annotations
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent
BRIEFS_DIR = _REPO_ROOT / "briefs"

# Permissive but safe call_id pattern: alphanumerics, dash, underscore.
# Anything else is rejected to keep us out of path-traversal territory.
_SAFE_CALL_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _validate_call_id(call_id: str) -> None:
    if not _SAFE_CALL_ID.fullmatch(call_id or ""):
        raise ValueError(f"unsafe call_id: {call_id!r}")


def _atomic_write(path: Path, data: str | bytes) -> None:
    """Write to <path>.tmp, then rename — survives mid-write crashes."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    mode = "wb" if isinstance(data, bytes) else "w"
    encoding = None if isinstance(data, bytes) else "utf-8"
    with open(tmp, mode, encoding=encoding) as f:
        f.write(data)
    os.replace(tmp, path)


def save_json(call_id: str, data: dict) -> Path:
    _validate_call_id(call_id)
    BRIEFS_DIR.mkdir(parents=True, exist_ok=True)
    path = BRIEFS_DIR / f"{call_id}.json"
    _atomic_write(path, json.dumps(data, indent=2, default=str))
    logger.info("wrote %s", path)
    return path


def save_markdown(call_id: str, md: str) -> Path:
    _validate_call_id(call_id)
    BRIEFS_DIR.mkdir(parents=True, exist_ok=True)
    path = BRIEFS_DIR / f"{call_id}.md"
    _atomic_write(path, md)
    logger.info("wrote %s", path)
    return path


def save_transcript(call_id: str, text: str) -> Path:
    """Persist the raw transcript so we can re-run extraction offline."""
    _validate_call_id(call_id)
    BRIEFS_DIR.mkdir(parents=True, exist_ok=True)
    path = BRIEFS_DIR / f"{call_id}.transcript.txt"
    _atomic_write(path, text)
    logger.info("wrote %s", path)
    return path


def read_markdown(call_id: str) -> str | None:
    _validate_call_id(call_id)
    path = BRIEFS_DIR / f"{call_id}.md"
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8")


def read_json(call_id: str) -> dict | None:
    _validate_call_id(call_id)
    path = BRIEFS_DIR / f"{call_id}.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def list_briefs() -> list[dict]:
    """Enumerate all saved briefs, newest first.

    Reads each `<call_id>.json` in the briefs/ directory and returns a list
    of summary dicts shaped for the index page renderer:

        {
            "call_id":          str,
            "mtime":            float,        # epoch seconds, for sort
            "started_at":       str | None,   # ISO-8601 if present in call_metadata
            "duration_sec":     int | None,
            "patient_name":     str | None,
            "visit_type":       str | None,   # e.g. "follow_up"
            "summary":          str | None,   # chief_complaint.summary
            "verbatim":         str | None,   # chief_complaint.verbatim
            "severity_worst":   int | None,
            "red_flag_count":   int,
            "red_flag_severity": str | None,  # highest severity among flags
            "completeness":     int | None,
            "has_transcript":   bool,
        }

    Sort key: prefer `started_at` (the actual call time) and fall back to the
    file's mtime when metadata is missing — both descending so the most
    recent intake lands at the top.

    Malformed JSON files are skipped with a warning, never raised — a single
    bad file shouldn't blank out the whole list.
    """
    if not BRIEFS_DIR.is_dir():
        return []

    items: list[dict] = []
    for path in BRIEFS_DIR.glob("*.json"):
        if path.suffix == ".tmp":  # mid-write artifact
            continue
        call_id = path.stem
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("could not read brief %s: %s", path.name, e)
            continue

        meta = data.get("call_metadata") or {}
        cc = data.get("chief_complaint") or {}
        hpi = data.get("hpi") or {}
        pid = data.get("patient_identifiers") or {}
        completeness = data.get("completeness") or {}
        red_flags = data.get("red_flags") or []

        # Highest red-flag severity (emergent > urgent > concern).
        sev_rank = {"emergent": 3, "urgent": 2, "concern": 1}
        max_flag_sev = None
        for rf in red_flags:
            s = rf.get("severity")
            if s and (max_flag_sev is None or sev_rank.get(s, 0) > sev_rank.get(max_flag_sev, 0)):
                max_flag_sev = s

        items.append({
            "call_id": call_id,
            "mtime": path.stat().st_mtime,
            "started_at": meta.get("started_at"),
            "duration_sec": meta.get("duration_sec"),
            "patient_name": pid.get("name") if isinstance(pid, dict) else None,
            "visit_type": pid.get("visit_type") if isinstance(pid, dict) else None,
            "summary": cc.get("summary"),
            "verbatim": cc.get("verbatim"),
            "severity_worst": hpi.get("severity_worst"),
            "red_flag_count": len(red_flags),
            "red_flag_severity": max_flag_sev,
            "completeness": completeness.get("score") if isinstance(completeness, dict) else None,
            "has_transcript": (BRIEFS_DIR / f"{call_id}.transcript.txt").is_file(),
        })

    def _sort_key(item: dict) -> float:
        # Use started_at when we have it; fall back to mtime. Both as epoch
        # seconds so a single key sorts cleanly. Newest first → negate.
        started = item.get("started_at")
        if started:
            try:
                return -datetime.fromisoformat(started.replace("Z", "+00:00")).timestamp()
            except ValueError:
                pass
        return -float(item.get("mtime") or 0.0)

    items.sort(key=_sort_key)
    return items


__all__ = [
    "BRIEFS_DIR",
    "save_json",
    "save_markdown",
    "save_transcript",
    "read_markdown",
    "read_json",
    "list_briefs",
]
