"""Recognise the files the hook wrote.

Retention and the sink both decide whether a daily file may be deleted or
appended to. They share these checks so ownership is settled the same way in
both places: by the records' own shape, never by the filename alone.
"""

from __future__ import annotations

import datetime as dt
import json
import typing as typ

from post_turn_quality_stop_hook.decision_log.constants import (
    LOG_FILE_PREFIX,
    LOG_FILE_SUFFIX,
    PRODUCER,
    RECORD_TYPES,
    SCHEMA_VERSION,
)

if typ.TYPE_CHECKING:
    from pathlib import Path

# A day's records stay well below this; the bound keeps a misnamed huge file
# from being read in full merely to decide ownership. A file larger than the
# bound cannot be verified, so it is preserved and never appended to.
_OWNERSHIP_READ_LIMIT = 8 * 1024 * 1024


def _log_file_date(path: Path) -> dt.date | None:
    """Parse the date encoded in a decision-log filename."""
    name = path.name
    if not (name.startswith(LOG_FILE_PREFIX) and name.endswith(LOG_FILE_SUFFIX)):
        return None
    encoded = name.removeprefix(LOG_FILE_PREFIX).removesuffix(LOG_FILE_SUFFIX)
    try:
        return dt.date.fromisoformat(encoded)
    except ValueError:
        return None


def _is_hook_record_line(line: bytes) -> bool:
    """Return whether ``line`` is a record the hook writes.

    A daily filename does not prove ownership: an unrelated untracked file can
    carry the same name, and Git holds no copy of it. Every record the hook
    writes carries the producer marker, one of a closed set of record types,
    and the common fields, so a file that merely happens to be JSON, or that
    uses a schema-like envelope of its own, is preserved.
    """
    try:
        record = json.loads(line)
    except ValueError:
        return False
    if not isinstance(record, dict):
        return False
    version = record.get("schema_version")
    return (
        isinstance(version, int)
        and version >= SCHEMA_VERSION
        and record.get("producer") == PRODUCER
        and record.get("record_type") in RECORD_TYPES
        and isinstance(record.get("run_id"), str)
        and isinstance(record.get("pid"), int)
        and isinstance(record.get("timestamp"), str)
    )


def _is_hook_record_content(content: bytes) -> bool:
    """Return whether ``content`` consists only of records the hook writes.

    Every line is checked, not just the first. A file that begins with a copied
    or stray hook record and continues with unrelated data is not the hook's
    file: appending to it would mix two writers into one line stream, and
    deleting it would destroy data Git cannot restore. Content that does not
    end with a newline is also rejected, because a torn final line cannot be
    verified as a complete record.
    """
    if not content.endswith(b"\n"):
        return False
    return all(_is_hook_record_line(line) for line in content.split(b"\n")[:-1])


def _is_hook_record(path: Path) -> bool:
    """Return whether a file holds only records the hook writes.

    A file that cannot be read, is not a regular file, or is larger than the
    verification bound is preserved. Only a regular file can be a record:
    opening a FIFO that happens to carry a daily name would block the hook
    indefinitely.
    """
    try:
        if not path.is_file():
            return False
        if path.stat().st_size > _OWNERSHIP_READ_LIMIT:
            return False
        content = path.read_bytes()
    except OSError:
        return False
    return _is_hook_record_content(content)
