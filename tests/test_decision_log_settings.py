"""Settings resolved from configuration, environment and defaults."""

from __future__ import annotations

import inspect
import os
import typing as typ
import uuid
from pathlib import Path

import pytest
from decision_log_support import (
    CONFIG_RETENTION,
    CORRELATION,
    DEFAULT_RETENTION,
    ENV_RETENTION,
    FALLBACK_RETENTION,
    _log_files,
    _settings,
)

from post_turn_quality_stop_hook import hook_composition
from post_turn_quality_stop_hook.config import Config, ConfigError
from post_turn_quality_stop_hook.decision_log import (
    DecisionLog,
    DecisionLogSettings,
    InvocationContext,
    RecordSink,
    WritePolicy,
    make_decision_log,
    parse_retention_days,
    settings_from_env,
)
from post_turn_quality_stop_hook.decision_log.adapters import (
    signal_name,
    terminal_commit,
)
from post_turn_quality_stop_hook.decision_log.clock import SystemClock
from post_turn_quality_stop_hook.diagnostics import warn
from post_turn_quality_stop_hook.hook_composition import (
    DecisionLogRun,
    ProcessSnapshot,
    apply_config_settings,
    start_decision_log,
)

# A retention value supplied through the injected mapping, never the process.
_INJECTED_RETENTION_DAYS = 3


def _compose(
    settings: DecisionLogSettings,
    context: InvocationContext,
    *,
    sink: RecordSink,
) -> DecisionLog | None:
    """Compose a log with the process-facing collaborators a root binds."""
    log = make_decision_log(
        settings,
        context,
        sink=sink,
        clock=SystemClock(),
        run_id_factory=lambda: uuid.uuid4().hex,
        pid=os.getpid(),
        signal_name=signal_name,
        terminal_commit=terminal_commit,
        on_error=warn,
    )
    if log is not None:
        # Production delivers the start record from the run's ``__enter__``,
        # after the signal guard is installed; this helper represents that
        # entered state.
        log.write_start()
    return log


class TestSettings:
    """Resolve settings from configuration and the environment."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (None, 14),
            ("", 14),
            ("  ", 14),
            ("not-a-number", 14),
            ("-3", 14),
            ("0", 0),
            ("7", 7),
            (" 21 ", 21),
        ],
    )
    def test_parse_retention_days(self, raw: str | None, expected: int) -> None:
        """Only non-negative integers override the default."""
        assert parse_retention_days(raw, 14) == expected

    def test_defaults_enable_logging(self, tmp_path: Path) -> None:
        """Logging is on by default beneath ``XDG_STATE_HOME``.

        The environment and home are explicit arguments: the composition edge
        reads the process once and passes that snapshot, so resolution has no
        hidden global dependency.
        """
        settings = settings_from_env(
            environ={"XDG_STATE_HOME": str(tmp_path / "xdg")},
            home=tmp_path / "home",
        )

        assert settings.enabled is True
        assert settings.state_home == tmp_path / "xdg"
        assert settings.retention_days == DEFAULT_RETENTION

    def test_state_home_falls_back_to_home(self, tmp_path: Path) -> None:
        """``~/.local/state`` is used when ``XDG_STATE_HOME`` is unset."""
        settings = settings_from_env(environ={}, home=tmp_path / "home")

        assert settings.state_home == tmp_path / "home" / ".local" / "state"

    def test_relative_state_home_falls_back_to_home(self, tmp_path: Path) -> None:
        """A relative ``XDG_STATE_HOME`` is ignored, as the XDG spec requires.

        Honouring it would put the log inside the repository that the hook is
        checking, and the uncommitted-changes gate would then block the run on
        the log file itself.
        """
        settings = settings_from_env(
            environ={"XDG_STATE_HOME": "relative/state"},
            home=tmp_path / "home",
        )

        assert settings.state_home == tmp_path / "home" / ".local" / "state"

    def test_config_values_apply(self, tmp_path: Path) -> None:
        """Configuration can disable logging and set retention."""
        settings = settings_from_env(
            Config(log_enabled=False, log_retention_days=CONFIG_RETENTION),
            environ={},
            home=Path("/home/test"),
        )

        assert settings.enabled is False
        assert settings.retention_days == CONFIG_RETENTION

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("1", True),
            ("true", True),
            ("yes", True),
            ("on", True),
            ("0", False),
            ("false", False),
            ("no", False),
            ("off", False),
        ],
    )
    def test_environment_toggle_overrides_config(
        self,
        raw: str,
        expected: bool,
    ) -> None:
        """``POST_TURN_LOG`` overrides the config value in either direction."""
        settings = settings_from_env(
            Config(log_enabled=not expected),
            environ={"POST_TURN_LOG": raw},
            home=Path("/home/test"),
        )

        assert settings.enabled is expected

    def test_unrecognized_toggle_keeps_config(self) -> None:
        """An unparseable toggle is ignored rather than guessed."""
        settings = settings_from_env(
            Config(log_enabled=False),
            environ={"POST_TURN_LOG": "maybe"},
            home=Path("/home/test"),
        )

        assert settings.enabled is False

    def test_environment_retention_overrides_config(self) -> None:
        """``POST_TURN_LOG_RETENTION_DAYS`` wins over the config value."""
        settings = settings_from_env(
            Config(log_retention_days=FALLBACK_RETENTION),
            environ={"POST_TURN_LOG_RETENTION_DAYS": str(ENV_RETENTION)},
            home=Path("/home/test"),
        )

        assert settings.retention_days == ENV_RETENTION

    def test_invalid_environment_retention_keeps_config(self) -> None:
        """An invalid environment retention falls back to the config value."""
        settings = settings_from_env(
            Config(log_retention_days=FALLBACK_RETENTION),
            environ={"POST_TURN_LOG_RETENTION_DAYS": "soon"},
            home=Path("/home/test"),
        )

        assert settings.retention_days == FALLBACK_RETENTION

    def test_environment_and_home_can_be_injected(self, tmp_path: Path) -> None:
        """Resolution reads the supplied mapping and home, not the process.

        The hook supplies its own environment and home at the composition
        boundary. This is that seam, so a test or an embedding caller can
        resolve settings without mutating the process environment.
        """
        settings = settings_from_env(
            environ={
                "XDG_STATE_HOME": str(tmp_path / "xdg"),
                "POST_TURN_LOG": "0",
                "POST_TURN_LOG_RETENTION_DAYS": "3",
            },
            home=tmp_path / "home",
        )

        assert settings.enabled is False
        assert settings.state_home == tmp_path / "xdg"
        assert settings.retention_days == _INJECTED_RETENTION_DAYS

    def test_disabled_settings_create_no_log(self, tmp_path: Path) -> None:
        """``make_decision_log`` returns ``None`` when logging is disabled.

        The sink is kept so the test proves the disabled factory wrote and
        pruned nothing, not merely that no file happened to appear.
        """
        sink = _RecordingSink()
        log = _compose(
            _settings(tmp_path, enabled=False),
            InvocationContext(
                correlation=CORRELATION,
                start_dir=Path("/repo"),
                base_ref="origin/main",
            ),
            sink=sink,
        )

        assert log is None
        assert sink.records == [], (
            f"a disabled factory must not write to its sink, but wrote {sink.records!r}"
        )
        assert sink.pruned == [], (
            f"a disabled factory must not prune through its sink, but pruned"
            f" {sink.pruned!r}"
        )
        assert _log_files(tmp_path / "state") == []


class _RecordingSink:
    """Keep records and prune calls without touching the filesystem."""

    def __init__(self) -> None:
        """Start with no records or prune calls."""
        self.records: list[dict[str, object]] = []
        self.pruned: list[tuple[object, int]] = []

    def write(self, record: object) -> None:
        """Record one written record."""
        self.records.append(dict(typ.cast("dict[str, object]", record)))

    def prune(self, today: object, retention_days: int) -> None:
        """Record one prune call."""
        self.pruned.append((today, retention_days))


def _context() -> InvocationContext:
    """Return the invocation context shared by the composition tests."""
    return InvocationContext(
        correlation=CORRELATION, start_dir=Path("/repo"), base_ref="origin/main"
    )


class TestNoHiddenProcessDefaults:
    """Settings never read the process on the caller's behalf."""

    def test_settings_have_no_home_default(self) -> None:
        """A missing state home is an error, not a read of the real home."""
        import dataclasses

        fields = {
            field.name: field for field in dataclasses.fields(DecisionLogSettings)
        }

        assert fields["state_home"].default is dataclasses.MISSING
        assert fields["state_home"].default_factory is dataclasses.MISSING, (
            "a default factory would let a caller log into an unintended home"
        )

    def test_settings_from_env_requires_the_snapshot(self) -> None:
        """The environment and home are required arguments, not globals."""
        parameters = inspect.signature(settings_from_env).parameters

        for name in ("environ", "home"):
            assert parameters[name].default is inspect.Parameter.empty, (
                f"{name} must be supplied by the composition edge"
            )

    def test_the_log_requires_its_process_collaborators(self) -> None:
        """The clock, identifiers, signal table and mask have no domain default."""
        parameters = inspect.signature(DecisionLog).parameters

        for name in (
            "clock",
            "run_id_factory",
            "pid",
            "signal_name",
            "terminal_commit",
        ):
            assert parameters[name].default is inspect.Parameter.empty, (
                f"{name} must be bound by the composition root"
            )

    def test_the_policy_requires_its_warning_channel(self) -> None:
        """A policy cannot silently warn through standard error on its own."""
        parameters = inspect.signature(WritePolicy).parameters

        assert parameters["on_error"].default is inspect.Parameter.empty, (
            "on_error must be bound by the composition root"
        )

    def test_the_appender_requires_its_warning_channel(self) -> None:
        """A locked appender cannot silently warn through standard error."""
        from post_turn_quality_stop_hook.decision_log.adapters import (
            LockedJsonlAppender,
        )

        parameters = inspect.signature(LockedJsonlAppender).parameters

        assert parameters["on_error"].default is inspect.Parameter.empty, (
            "on_error must be bound by the composition root"
        )


class TestCompositionSnapshot:
    """The hook reads the process environment and home once per run."""

    def test_start_uses_the_supplied_snapshot(self, tmp_path: Path) -> None:
        """Settings resolve from the snapshot, not the live process.

        The snapshot names both the opt-out and the destination beneath it, so
        the assertion describes where the log would have written rather than an
        unrelated path.
        """
        state_home = tmp_path / "state"
        process = ProcessSnapshot(
            environ={
                "POST_TURN_LOG": "0",
                "XDG_STATE_HOME": str(state_home),
            },
            home=tmp_path / "home",
            cwd=None,
        )

        log = start_decision_log(_context(), 0.0, process=process)

        assert log is None, "the snapshot opted out before the log was created"
        assert _log_files(state_home) == [], (
            "the snapshot's opt-out must not create a log beneath its state home"
        )

    def test_start_reads_the_state_home_from_the_snapshot(self, tmp_path: Path) -> None:
        """The snapshot's XDG state home locates the log directory."""
        state_home = tmp_path / "xdg"
        process = ProcessSnapshot(
            environ={"XDG_STATE_HOME": str(state_home)},
            home=tmp_path / "home",
            cwd=None,
        )

        log = start_decision_log(_context(), 0.0, process=process)
        assert log is not None

        log.finish()

        assert len(_log_files(state_home)) == 1, (
            "the log is written beneath the snapshot's state home"
        )

    def test_apply_config_uses_the_supplied_snapshot(self, tmp_path: Path) -> None:
        """The later configuration read reuses the same snapshot."""
        sink = _RecordingSink()
        log = _compose(_settings(tmp_path), _context(), sink=sink)
        assert log is not None
        process = ProcessSnapshot(
            environ={"POST_TURN_LOG_RETENTION_DAYS": "3"},
            home=tmp_path / "home",
            cwd=None,
        )

        apply_config_settings(log, Config(log_retention_days=9), process=process)

        assert sink.pruned, "the configured window schedules a prune"
        assert sink.pruned[-1][1] == _INJECTED_RETENTION_DAYS, (
            "the snapshot's environment override decides the window"
        )

    def test_apply_config_opt_out_uses_the_supplied_snapshot(
        self, tmp_path: Path
    ) -> None:
        """An environment opt-out in the snapshot disables the log."""
        sink = _RecordingSink()
        log = _compose(_settings(tmp_path), _context(), sink=sink)
        assert log is not None
        process = ProcessSnapshot(
            environ={"POST_TURN_LOG": "0"},
            home=tmp_path / "home",
            cwd=None,
        )

        apply_config_settings(log, Config(log_enabled=True), process=process)

        finishes = [
            record for record in sink.records if record["record_type"] == "finish"
        ]
        assert [record["decision"] for record in finishes] == ["disabled"], (
            "the snapshot's opt-out writes the terminal disabled record"
        )

    def test_a_configuration_error_does_not_prune(self, tmp_path: Path) -> None:
        """An unknown retention window never deletes files it might keep.

        A malformed ``--config`` makes the authoritative window and opt-out
        unknown. Pruning then with the startup default could permanently
        remove a valid log the intended window would retain, so the failed load
        skips retention and leaves the log open for the configuration block.
        """
        sink = _RecordingSink()
        log = _compose(_settings(tmp_path), _context(), sink=sink)
        assert log is not None
        process = ProcessSnapshot(
            environ={"POST_TURN_LOG_RETENTION_DAYS": "2"},
            home=tmp_path / "home",
            cwd=None,
        )
        before = list(sink.records)

        apply_config_settings(log, ConfigError("bad configuration"), process=process)

        assert sink.pruned == [], (
            "a failed configuration load must not prune with the fallback window,"
            f" but pruned {sink.pruned!r}"
        )
        assert sink.records == before, (
            "the log must stay open so the hook can record the configuration block"
        )

    def test_a_disabled_run_exposes_no_recorder(self, tmp_path: Path) -> None:
        """An opt-out selects the no-op observer for the rest of the run."""
        sink = _RecordingSink()
        log = _compose(_settings(tmp_path), _context(), sink=sink)
        assert log is not None
        process = ProcessSnapshot(environ={}, home=tmp_path / "home", cwd=None)

        with DecisionLogRun(log, process=process) as run:
            assert run.recorder is log, (
                "an enabled run exposes the recorder the pipeline writes through"
            )
            run.apply_config(Config(log_enabled=False))
            assert run.recorder is None, (
                "an opted-out run must expose no recorder, so the pipeline skips"
                " repository queries whose results could never be written"
            )

        finishes = [
            record for record in sink.records if record["record_type"] == "finish"
        ]
        assert [record["decision"] for record in finishes] == ["disabled"]

    def test_the_signal_guard_is_installed_before_the_start_record(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The guard is in place before a start record becomes visible."""
        events: list[str] = []
        sink = _RecordingSink()
        log = make_decision_log(
            _settings(tmp_path),
            _context(),
            sink=sink,
            clock=SystemClock(),
            run_id_factory=lambda: "run-1",
            pid=1,
            signal_name=signal_name,
            terminal_commit=terminal_commit,
            on_error=warn,
        )
        assert log is not None

        class _RecordingGuard(hook_composition.SignalGuard):
            """Record that the guard was installed."""

            def __enter__(self) -> hook_composition.SignalGuard:
                """Record that the guard was installed, then install it."""
                events.append("guard")
                return super().__enter__()

        monkeypatch.setattr(hook_composition, "SignalGuard", _RecordingGuard)
        original_write_start = log.write_start

        def _write_start() -> None:
            """Record that the start record was written, then write it."""
            events.append("start")
            original_write_start()

        monkeypatch.setattr(log, "write_start", _write_start)

        with DecisionLogRun(
            log,
            process=ProcessSnapshot(environ={}, home=tmp_path, cwd=None),
        ):
            pass

        assert events[:2] == ["guard", "start"], (
            "the signal guard must be installed before the start record is"
            f" delivered, but the order was {events!r}"
        )
        assert next(record["record_type"] for record in sink.records) == "start"

    def test_a_finish_record_never_precedes_its_start(self, tmp_path: Path) -> None:
        """Construction delivers nothing until a terminal command asks for it."""
        sink = _RecordingSink()
        log = make_decision_log(
            _settings(tmp_path),
            _context(),
            sink=sink,
            clock=SystemClock(),
            run_id_factory=lambda: "run-1",
            pid=1,
            signal_name=signal_name,
            terminal_commit=terminal_commit,
            on_error=warn,
        )
        assert log is not None
        assert sink.records == [], (
            "constructing the log must not deliver its start record"
        )

        log.finish(exit_code=0)

        kinds = [record["record_type"] for record in sink.records]
        assert kinds[0] == "start", (
            f"the finish record must follow its start, got {kinds!r}"
        )
        assert kinds[-1] == "finish"


class TestProcessSnapshot:
    """The process snapshot is read defensively."""

    def test_an_unresolvable_home_leaves_logging_inert(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A home that cannot be resolved must not abort the hook."""
        message = "home is unavailable"

        def _raise() -> Path:
            """Fail the home lookup the way a broken passwd entry does."""
            raise RuntimeError(message)

        monkeypatch.setattr(Path, "home", _raise)

        snapshot = ProcessSnapshot.read()

        assert snapshot.home == Path(), (
            "a relative home is refused by the write policy, so logging is inert"
        )

    def test_the_working_directory_is_snapshotted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The edge captures the directory later resolution reads."""
        monkeypatch.chdir(tmp_path)

        snapshot = ProcessSnapshot.read()

        assert snapshot.cwd == tmp_path.resolve(), (
            "the start directory must come from the snapshot, not a later read"
        )

    def test_an_unreadable_working_directory_is_recorded_as_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A removed working directory is recorded, not raised."""
        message = "working directory removed"

        def _raise() -> Path:
            """Fail the working-directory read as a removed directory does."""
            raise OSError(message)

        monkeypatch.setattr(Path, "cwd", _raise)

        snapshot = ProcessSnapshot.read()

        assert snapshot.cwd is None, (
            "an absent directory must leave the start-directory fallback empty"
        )
