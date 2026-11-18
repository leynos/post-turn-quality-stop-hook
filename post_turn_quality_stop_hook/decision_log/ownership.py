"""Recognise the files the hook wrote.

Retention and the sink both decide whether a daily file may be deleted or
appended to. They share these checks so ownership is settled the same way in
both places: by the records' own shape, never by the filename alone.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import typing as typ

from post_turn_quality_stop_hook.decision_log.constants import (
    LOG_FILE_PREFIX,
    LOG_FILE_SUFFIX,
    PRODUCER,
    RECORD_TYPES,
    SCHEMA_VERSION,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

# Verification holds one partial line at a time, so peak memory does not grow
# with the daily file. A record the hook writes stays far below the per-line
# bound; a longer line is treated as foreign content rather than held in memory
# to decide.
_VERIFY_CHUNK_BYTES = 64 * 1024
_MAX_RECORD_BYTES = 1024 * 1024


def _log_file_date(path: Path) -> dt.date | None:
    """Parse the date encoded in a decision-log filename."""
    name = path.name
    if not (name.startswith(LOG_FILE_PREFIX) and name.endswith(LOG_FILE_SUFFIX)):
        return None
    encoded = name.removeprefix(LOG_FILE_PREFIX).removesuffix(LOG_FILE_SUFFIX)
    try:
        parsed = dt.date.fromisoformat(encoded)
    except ValueError:
        return None
    # ``fromisoformat`` also accepts unpadded forms such as ``2026-1-5``, but
    # the hook only ever writes the canonical form. A name it would not write
    # is not proof that it wrote the file.
    return parsed if parsed.isoformat() == encoded else None


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
    record_type = record.get("record_type")
    return (
        isinstance(version, int)
        and version >= SCHEMA_VERSION
        and record.get("producer") == PRODUCER
        # Check the type first: membership in the closed set raises on a list
        # or object, and foreign JSON may hold either.
        and isinstance(record_type, str)
        and record_type in RECORD_TYPES
        and isinstance(record.get("run_id"), str)
        and isinstance(record.get("pid"), int)
        and isinstance(record.get("timestamp"), str)
    )


def _is_hook_record_stream(chunks: cabc.Iterable[bytes]) -> bool:
    """Return whether ``chunks`` consist only of records the hook writes.

    Every line is checked, not just the first. A file that begins with a copied
    or stray hook record and continues with unrelated data is not the hook's
    file: appending to it would mix two writers into one line stream, and
    deleting it would destroy data Git cannot restore. Content that does not
    end with a newline is also rejected, because a torn final line cannot be
    verified as a complete record.
    """
    pending = b""
    verified = False
    for chunk in chunks:
        pending += chunk
        lines = pending.split(b"\n")
        pending = lines.pop()
        if len(pending) > _MAX_RECORD_BYTES:
            return False
        for line in lines:
            # The newline can fall within one chunk, so a complete line needs
            # the same bound as an accumulating one.
            if len(line) > _MAX_RECORD_BYTES:
                return False
            verified = True
            if not _is_hook_record_line(line):
                return False
    # The trailing newline is part of the format, so a leftover partial line
    # means the file was torn. An empty file proves nothing either.
    return verified and not pending


def _is_hook_record_descriptor(descriptor: int) -> bool:
    """Return whether an open file holds only records the hook writes.

    The file is read from the start in bounded chunks. The caller holds the
    log's exclusive lock, so moving the descriptor's offset cannot disturb
    another writer, and records are appended with ``O_APPEND`` regardless.
    """

    def chunks() -> cabc.Iterator[bytes]:
        """Yield the file's content in bounded pieces."""
        os.lseek(descriptor, 0, os.SEEK_SET)
        while chunk := os.read(descriptor, _VERIFY_CHUNK_BYTES):
            yield chunk

    return _is_hook_record_stream(chunks())


def _is_hook_record(path: Path) -> bool:
    """Return whether a file holds only records the hook writes.

    A file that cannot be read or is not a regular file is preserved. Only a
    regular file can be a record: opening a FIFO that happens to carry a daily
    name would block the hook indefinitely.
    """
    try:
        if not path.is_file():
            return False
        with path.open("rb") as stream:
            return _is_hook_record_stream(
                iter(lambda: stream.read(_VERIFY_CHUNK_BYTES), b"")
            )
    except OSError:
        return False
