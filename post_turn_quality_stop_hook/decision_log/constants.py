"""Constants shared by the decision-log modules."""

from __future__ import annotations

from importlib import metadata
from pathlib import Path

SCHEMA_VERSION = 1
DEFAULT_RETENTION_DAYS = 14
MAX_REASON_CHARS = 500
# Correlation values come from the hook payload, so their length is not the
# hook's to assume. Bounding each string keeps ordinary identifiers intact while
# guaranteeing that no record can approach the reader's per-line limit.
MAX_CORRELATION_CHARS = 4096
LOG_SUBDIR = Path("post-turn-quality-stop-hook") / "log"
LOG_FILE_PREFIX = "hook-"
LOG_FILE_SUFFIX = ".jsonl"
# Every record names its producer. Retention and the sink use this marker, not
# the filename, to decide whether a file belongs to the hook, so an unrelated
# file that happens to carry a daily name is preserved.
PRODUCER = "post-turn-quality-stop-hook"
# The record types the hook writes. A file whose first record names any other
# type was not written by this hook.
RECORD_TYPES = frozenset({"start", "progress", "finish"})
GATE_ORDER = (
    "quality_checks",
    "uncommitted_changes",
    "unpushed_commits",
    "pr_rebase",
)
TRUTHY_TOKENS = frozenset({"1", "true", "yes", "on"})
FALSY_TOKENS = frozenset({"0", "false", "no", "off"})
# One record must stay small enough to be verified line by line. A reader that
# meets a longer line treats the file as foreign, so the writer bounds its own
# records well below the reader's limit: the reader can then still recognise a
# genuine file whose line was padded by an older or experimental writer.
MAX_RECORD_BYTES = 1024 * 1024
RECORD_HEADROOM_BYTES = 4 * 1024


def _package_version() -> str:
    """Return the installed package version, or ``unknown`` when unavailable."""
    try:
        return metadata.version("post-turn-quality-stop-hook")
    except metadata.PackageNotFoundError:
        return "unknown"


HOOK_VERSION = _package_version()
