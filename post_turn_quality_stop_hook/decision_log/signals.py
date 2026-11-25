"""Best-effort finish records and signal masking for the decision log."""

from __future__ import annotations

import contextlib
import os
import signal
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import types


@contextlib.contextmanager
def terminal_commit() -> cabc.Iterator[None]:
    """Hold catchable signals while a terminal record and its flag commit.

    A terminal record is appended and then the delivery flag is published. A
    signal delivered between those two steps would find delivery still open
    and append a second terminal record. The mask closes that gap for the
    signals the guard handles: an arrival during the commit stays pending until
    the commit has published the flag, and the handler then finds the run
    already finished. An arrival before the mask is taken is delivered
    normally, so an interruption before the commit still records itself.

    The mask is per thread. The hook writes from its main thread, which is also
    where its signal handlers run, so that is the thread whose commit needs
    protecting. Platforms without ``pthread_sigmask`` keep the previous
    best-effort ordering.
    """
    blocker = getattr(signal, "pthread_sigmask", None)
    catchable = _catchable_signals()
    if blocker is None or not catchable:
        yield
        return
    try:
        previous = blocker(signal.SIG_BLOCK, catchable)
    except OSError, ValueError:
        yield
        return
    try:
        yield
    finally:
        blocker(signal.SIG_SETMASK, previous)


class SignalGuard:
    """Install best-effort handlers for catchable termination signals.

    Parameters
    ----------
    on_signal
        Callback invoked with the signal number before the signal is
        re-raised. It must not raise.

    """

    def __init__(self, on_signal: cabc.Callable[[int], None]) -> None:
        """Store the callback invoked before a caught signal is re-raised."""
        self._on_signal = on_signal
        self._installed: list[tuple[int, object]] = []

    def _handle(self, signum: int, _frame: types.FrameType | None) -> None:
        """Record the interruption and re-raise the signal."""
        try:
            self._on_signal(signum)
        finally:
            # SIGINT must unwind through ``subprocess.run`` so a build child is
            # terminated and reaped. Forcing ``SIG_DFL`` exits immediately and
            # orphans that child, so restore Python's default interrupt handler
            # instead. Other signals keep forcing the default disposition.
            handler = (
                signal.default_int_handler
                if signum == getattr(signal, "SIGINT", None)
                else signal.SIG_DFL
            )
            with contextlib.suppress(ValueError, OSError):
                signal.signal(signum, handler)
            os.kill(os.getpid(), signum)

    def __enter__(self) -> SignalGuard:
        """Install handlers, preserving dispositions the caller chose."""
        for signum in _catchable_signals():
            try:
                previous = signal.getsignal(signum)
                # Respect a disposition the caller deliberately chose: a
                # signal ignored before the hook started must stay ignored. A
                # ``None`` handler comes from native code and cannot be
                # restored by ``signal.signal``, so leave it alone too.
                if previous is None or previous == signal.SIG_IGN:
                    continue
                signal.signal(signum, self._handle)
            except ValueError, OSError:
                continue
            self._installed.append((signum, previous))
        return self

    def __exit__(self, *_exc: object) -> None:
        """Restore the handlers that were previously installed."""
        for signum, previous in self._installed:
            try:
                signal.signal(signum, typ.cast("signal.Handlers", previous))
            except ValueError, OSError:
                continue
        self._installed.clear()


def _catchable_signals() -> tuple[int, ...]:
    """Return termination signals that can carry a finish record."""
    values: list[int] = []
    for name in ("SIGTERM", "SIGINT", "SIGHUP"):
        value = getattr(signal, name, None)
        if isinstance(value, int):
            values.append(value)
    return tuple(values)
