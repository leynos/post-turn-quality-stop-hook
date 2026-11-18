"""Constants shared by the decision-log modules."""

from __future__ import annotations

from importlib import metadata
from pathlib import Path

SCHEMA_VERSION = 1
DEFAULT_RETENTION_DAYS = 14
MAX_REASON_CHARS = 500
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


def _package_version() -> str:
    """Return the installed package version, or ``unknown`` when unavailable."""
    try:
        return metadata.version("post-turn-quality-stop-hook")
    except metadata.PackageNotFoundError:
        return "unknown"


HOOK_VERSION = _package_version()
