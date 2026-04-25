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


def _ensure_dir() -> None:
    BRIEFS_DIR.mkdir(parents=True, exist_ok=True)


def _atomic_write(path: Path, data: str | bytes) -> None:
    """Write to <path>.tmp, then rename. Survives mid-write crashes."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    mode = "wb" if isinstance(data, bytes) else "w"
    encoding = None if isinstance(data, bytes) else "utf-8"
    with open(tmp, mode, encoding=encoding) as f:
        f.write(data)
    os.replace(tmp, path)


def save_json(call_id: str, data: dict) -> Path:
    """Write the validated brief as JSON. Returns the path written."""
    _validate_call_id(call_id)
    _ensure_dir()
    path = BRIEFS_DIR / f"{call_id}.json"
    _atomic_write(path, json.dumps(data, indent=2, default=str))
    logger.info("wrote %s", path)
    return path


def save_markdown(call_id: str, md: str) -> Path:
    """Write the clinician-facing Markdown brief."""
    _validate_call_id(call_id)
    _ensure_dir()
    path = BRIEFS_DIR / f"{call_id}.md"
    _atomic_write(path, md)
    logger.info("wrote %s", path)
    return path


def save_transcript(call_id: str, text: str) -> Path:
    """Write the raw transcript that produced the brief — useful for debugging
    and for re-running extraction offline."""
    _validate_call_id(call_id)
    _ensure_dir()
    path = BRIEFS_DIR / f"{call_id}.transcript.txt"
    _atomic_write(path, text)
    logger.info("wrote %s", path)
    return path


def read_markdown(call_id: str) -> str | None:
    """Return the Markdown brief for `call_id`, or None if not found."""
    _validate_call_id(call_id)
    path = BRIEFS_DIR / f"{call_id}.md"
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8")


__all__ = [
    "BRIEFS_DIR",
    "save_json",
    "save_markdown",
    "save_transcript",
    "read_markdown",
]
