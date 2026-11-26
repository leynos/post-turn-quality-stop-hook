"""Locked, single-write appends to a daily JSON Lines file.

The adapter owns everything that touches the file: creation, both locks, the
permission narrowing, and the one ``O_APPEND`` write that makes a record
atomic. It decides nothing about *whether* a file may be used; the policy it is
handed answers that while the exclusive lock is held. Keeping the two apart
lets each be tested, and replaced, without the other.
"""

from __future__ import annotations

import dataclasses
import os
import threading
import typing as typ

from post_turn_quality_stop_hook.decision_log import platform_support
from post_turn_quality_stop_hook.decision_log.stats import StorageStats
from post_turn_quality_stop_hook.decision_log.timing import _elapsed_ms
from post_turn_quality_stop_hook.diagnostics import warn

if typ.TYPE_CHECKING:
    from pathlib import Path

    from post_turn_quality_stop_hook.decision_log.clock import Clock
    from post_turn_quality_stop_hook.decision_log.policy import FileClaim, FileOwnership

# Appending must never stall the hook. Another run holds the lock only for one
# short write, so a lock that is still held after this long belongs to
# something else, and the record is dropped rather than waited for.
_LOCK_TIMEOUT_SECONDS = 2.0
_LOCK_RETRY_SECONDS = 0.01
# A run creates the daily file with ``O_EXCL`` before it can lock and write it,
# so a concurrent run can find that file zero-length and must not mistake it for
# a file the hook did not write. The window is a few instructions long: a
# zero-length file is re-checked a bounded number of times before the normal
# ownership check is allowed to refuse it. A genuinely empty file, such as one
# a user created at the daily path, stays empty and is refused as before.
_EMPTY_WAIT_RETRIES = 4
_EMPTY_RETRY_SECONDS = 0.01
# ``os.open`` is binary on POSIX; Windows needs the flag to stop a text-mode
# translation of every newline we write.
_BINARY_FLAG = getattr(os, "O_BINARY", 0)
# The daily file is always a regular file the hook creates or claims. Following
# a symbolic link planted at its name would append through a path the location
# check never saw, so the link is refused instead.
_NOFOLLOW_FLAG = getattr(os, "O_NOFOLLOW", 0)


class AppendPolicy(typ.Protocol):
    """The decision the adapter asks for while it holds the lock.

    The adapter opens the file and takes the exclusive lock before asking, so a
    policy can answer from the claim it is given and cannot race with another
    writer. The method is command-shaped because a permitting answer records
    this run's claim on the file.

    ``claim`` is the adapter's own answer about the locked descriptor. Passing
    it rather than the descriptor keeps every ``os.fstat`` and every read on
    the adapter's side of the boundary, so a policy is a pure decision over
    values.

    ``location`` is the pathname the append actually wrote through: the name
    anchored to the opened log directory, not the earlier daily pathname. A
    policy that asks Git an ownership question asks it about this location, so
    its answer describes the file being appended to even if a parent pathname
    was replaced after the directory was opened.
    """

    def claim_append(
        self, path: Path, claim: FileClaim, *, created: bool, location: Path
    ) -> bool:
        """Claim the open file for this run's records, or refuse it."""
        ...


@dataclasses.dataclass(frozen=True, slots=True)
class AppendResult:
    """What one append attempt did.

    Attributes
    ----------
    wrote
        Whether the record was written in full.
    lock_wait_ms
        Time spent waiting for either lock, successful or not.

    """

    wrote: bool
    lock_wait_ms: int


class LockedJsonlAppender:
    """Create, lock, and append to a daily log file.

    Parameters
    ----------
    clock
        Time source for the bounded lock retry and its timings.
    ownership
        Ownership port that turns the locked descriptor into a claim for the
        policy. It is required, so the adapter never decides ownership itself
        and a test substitutes the answers it wants.
    stats
        Telemetry collector for write failures. A private collector is used
        when none is supplied.
    on_error
        Callback for failures. It receives a fixed message with no path and no
        exception text.

    """

    def __init__(
        self,
        clock: Clock,
        *,
        ownership: FileOwnership,
        stats: StorageStats | None = None,
        on_error: typ.Callable[[str], None] = warn,
    ) -> None:
        """Store the clock, ownership port, telemetry collector, and error sink."""
        self._clock = clock
        self._ownership = ownership
        self._stats = stats or StorageStats()
        self._on_error = on_error
        # The in-process lock serialises threads, and its owner identifies a
        # same-thread re-entry from a signal handler.
        self._lock = threading.Lock()
        self._lock_owner: int | None = None
        # Once one record has waited the full timeout, the daily file most
        # likely belongs to another process for the rest of this run. Later
        # records try once and give up instead of paying the same wait again.
        self._timed_out = False
        # A failed rollback leaves a torn line in the file. Nothing this run
        # writes afterwards could be verified as a complete record, so the
        # adapter stops appending rather than adding to a damaged stream.
        self._unusable = False
        # Whether this thread is inside ``append``, and the descriptor of a
        # write that has already been approved. A catchable signal runs on the
        # interrupted thread, so it must be recognised per thread: a second
        # descriptor would conflict with the ``flock`` the outer frame holds.
        self._thread_state = threading.local()

    @property
    def stats(self) -> StorageStats:
        """Telemetry collector this adapter records into."""
        return self._stats

    def append(
        self,
        path: Path,
        line: str,
        policy: AppendPolicy,
        *,
        location: Path | None = None,
    ) -> AppendResult:
        """Append ``line`` to ``path`` as one record.

        ``path`` names the daily file for the policy, which keys its ownership
        claims by that stable name. ``location`` is the pathname opened for the
        write itself: the sink passes a name anchored to the directory it
        validated, so replacing a parent pathname after validation cannot
        redirect the creation. It defaults to ``path``.

        The file is created with an exclusive open so the caller learns whether
        this run made it, then locked, then offered to ``policy``. A zero-length
        file is rechecked briefly first, because a concurrent run creates the
        file before it can lock and write it. Only a file the policy approves
        receives the record.
        """
        data = (line + "\n").encode("utf-8")
        if self._unusable:
            self._stats.fail_write("OSError")
            self._on_error(
                "the decision log is unusable after a failed rollback; not "
                "writing the record (OSError)"
            )
            return AppendResult(wrote=False, lock_wait_ms=0)
        if getattr(self._thread_state, "in_append", False):
            return self._append_from_signal(data)
        self._thread_state.in_append = True
        try:
            return self._append_under_lock(path, location or path, data, policy)
        finally:
            self._thread_state.in_append = False

    def _append_from_signal(self, data: bytes) -> AppendResult:
        """Write a record from a handler that interrupted this thread's append.

        The handler runs outside the in-process lock but while the ``flock``
        on the outer descriptor is still held, so a second descriptor would
        wait out the whole timeout while the interrupted frame cannot close
        the first. Once the file is approved for the outer write, the
        interrupted record is written through that active descriptor instead:
        it stays under the same lock and costs no wait. Before approval there
        is no descriptor that may be written to, so the record is refused
        immediately rather than waited for.
        """
        descriptor = getattr(self._thread_state, "descriptor", None)
        if descriptor is None:
            self._stats.fail_write("BlockingIOError")
            self._on_error(
                "the decision log is busy; not writing the record (BlockingIOError)"
            )
            return AppendResult(wrote=False, lock_wait_ms=0)
        return AppendResult(wrote=self._write(descriptor, data), lock_wait_ms=0)

    def _append_under_lock(
        self,
        path: Path,
        location: Path,
        data: bytes,
        policy: AppendPolicy,
    ) -> AppendResult:
        """Open, lock, approve, and write one record."""
        waited = 0
        attempt = 0
        while True:
            descriptor, created = self._open(location)
            try:
                acquired, attempt_wait = self._lock_file(descriptor)
                waited += attempt_wait
                if not acquired:
                    self._stats.fail_write("BlockingIOError")
                    self._on_error(
                        "the decision log is busy; not writing the record "
                        "(BlockingIOError)"
                    )
                    return AppendResult(wrote=False, lock_wait_ms=waited)
                # A file this run created, a file with content, or one that
                # stayed empty through every recheck goes to the policy. Only a
                # zero-length file that another run may still be filling waits.
                if self._ownership_due(descriptor, created=created, attempt=attempt):
                    # The adapter owns every descriptor operation: the injected
                    # port turns the descriptor into a claim, and the policy
                    # receives that value rather than the descriptor.
                    if not policy.claim_append(
                        path,
                        self._ownership.claim(descriptor),
                        created=created,
                        location=location,
                    ):
                        return AppendResult(wrote=False, lock_wait_ms=waited)
                    try:
                        # The mode above applies only to a new file. Tighten an
                        # existing one too, so a log created by an older
                        # version or another process cannot stay readable by
                        # other users.
                        platform_support.tighten_permissions(descriptor)
                        # Publish only after tightening. A signal handler that
                        # interrupts the write reuses this descriptor, and it
                        # must not be able to append through a file whose mode
                        # has not been restricted yet.
                        self._thread_state.descriptor = descriptor
                        wrote = self._write(descriptor, data)
                    finally:
                        self._thread_state.descriptor = None
                    return AppendResult(wrote=wrote, lock_wait_ms=waited)
            finally:
                os.close(descriptor)
            attempt += 1
            self._clock.sleep(_EMPTY_RETRY_SECONDS)

    def _ownership_due(self, descriptor: int, *, created: bool, attempt: int) -> bool:
        """Return whether the ownership check should decide the file now.

        Waiting is only for a zero-length file another run may still be
        filling. A file this run created needs no check, and a file that stayed
        empty through the bounded rechecks is not mid-creation.
        """
        if created or attempt >= _EMPTY_WAIT_RETRIES:
            return True
        return not self._is_empty(descriptor)

    @staticmethod
    def _is_empty(descriptor: int) -> bool:
        """Return whether the locked file has no content.

        A file whose size cannot be read is reported as non-empty, so the
        policy's own error handling decides what to do with it.
        """
        try:
            return os.fstat(descriptor).st_size == 0
        except OSError:
            return False

    @staticmethod
    def _open(path: Path) -> tuple[int, bool]:
        """Open the daily file, reporting whether this run created it.

        Creation is detected with ``O_CREAT | O_EXCL`` rather than by size so a
        pre-existing empty file is not mistaken for this run's own. A created
        file belongs to this run by definition; a pre-existing one is claimed
        only after every line is verified as a hook record. A symbolic link at
        the name is refused rather than followed: the location check saw the
        name, not whatever the link resolves to.
        """
        try:
            descriptor = os.open(
                path,
                os.O_RDWR
                | os.O_APPEND
                | os.O_CREAT
                | os.O_EXCL
                | _NOFOLLOW_FLAG
                | _BINARY_FLAG,
                0o600,
            )
        except FileExistsError:
            return (
                os.open(
                    path,
                    os.O_RDWR | os.O_APPEND | _NOFOLLOW_FLAG | _BINARY_FLAG,
                ),
                False,
            )
        return descriptor, True

    def _acquire_process_lock(self) -> bool:
        """Take the in-process lock, refusing same-thread re-entry.

        A catchable signal runs its Python handler on the main thread. If the
        handler records an ``interrupted`` finish while that same thread is
        inside :meth:`append`, waiting for the lock would wait on itself for the
        whole timeout. Refusing loses only a record the lock holder is already
        writing, and lets the handler re-raise the signal promptly.
        """
        if self._lock_owner == threading.get_ident():
            return False
        if not self._lock.acquire(timeout=_LOCK_TIMEOUT_SECONDS):
            return False
        self._lock_owner = threading.get_ident()
        return True

    def _release_process_lock(self) -> None:
        """Release the in-process lock taken by :meth:`_acquire_process_lock`."""
        self._lock_owner = None
        self._lock.release()

    def _lock_file(self, descriptor: int) -> tuple[bool, int]:
        """Take both locks, returning success and the time spent waiting.

        The caller cannot wait indefinitely: the hook must keep running even if
        some other process holds the file. A short bounded retry still absorbs
        the normal case of a concurrent run finishing its single append. After
        one full wait, later attempts are non-blocking.
        """
        started = self._clock.monotonic()
        acquired = self._acquire_process_lock()
        if not acquired:
            return False, _elapsed_ms(started, self._clock.monotonic())
        try:
            deadline = self._clock.monotonic() + _LOCK_TIMEOUT_SECONDS
            while True:
                try:
                    platform_support.lock_exclusive(descriptor)
                except BlockingIOError:
                    if self._timed_out:
                        return False, _elapsed_ms(started, self._clock.monotonic())
                    if self._clock.monotonic() >= deadline:
                        self._timed_out = True
                        return False, _elapsed_ms(started, self._clock.monotonic())
                    self._clock.sleep(_LOCK_RETRY_SECONDS)
                    continue
                return True, _elapsed_ms(started, self._clock.monotonic())
        finally:
            self._release_process_lock()

    def _write(self, descriptor: int, data: bytes) -> bool:
        """Write ``data`` once, restoring the verified length if it is short.

        A second append could interleave with another hook's record, so a short
        write is never retried. The length captured under the lock is restored
        instead, which removes the partial line: the next invocation then still
        verifies the file as one the hook wrote and can append to it, rather
        than rejecting it as foreign for the rest of the day.
        """
        verified_size = os.fstat(descriptor).st_size
        if os.write(descriptor, data) == len(data):
            return True
        self._stats.fail_write("ShortWrite")
        try:
            os.ftruncate(descriptor, verified_size)
        except OSError:
            self._unusable = True
            self._on_error(
                "could not restore the decision log after a short write; "
                "not writing further records (OSError)"
            )
            return False
        self._on_error("could not write the complete decision log record (ShortWrite)")
        return False
