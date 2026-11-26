"""Property tests for build-driver selection under controlled inputs.

`select_build_driver` is a decision function. It reads two manifest
presences, two executable availabilities, and the requested driver name, and
must answer with exactly one of three shapes: a runnable driver, an explained
failure, or the "nothing to build" skip. Each generated scenario fixes those
inputs, so these properties describe the decision rather than the machine the
suite happens to run on — the ambient `PATH` and home directory take no part.
"""

from __future__ import annotations

import contextlib
import dataclasses
import shutil
import tempfile
import typing as typ
from pathlib import Path
from unittest import mock

from hypothesis import given
from hypothesis import strategies as st

from post_turn_quality_stop_hook import driver as driver_mod
from post_turn_quality_stop_hook import state as state_mod
from post_turn_quality_stop_hook.errors import HookError

if typ.TYPE_CHECKING:
    import collections.abc as cabc

SUPPORTED_REQUESTS = ("auto", "make", "netsuke")

SUPPORTED_DRIVER_NAMES = frozenset({"make", "netsuke"})

# Disjoint pools, so a scenario's two tools never share one availability
# record and each driver's binary stays identifiable in the result.
NETSUKE_BINARIES = ("netsuke", "netsuke-dev", "/opt/netsuke/bin/netsuke")
MAKE_BINARIES = ("make", "gmake", "/usr/bin/make")

MAX_OUT = 12000

#: Requests that normalise to none of the supported driver names. The alphabet
#: cannot spell a supported name, so no candidate is filtered after the draw.
UNSUPPORTED_REQUESTS = st.one_of(
    st.sampled_from(("", "cmake", "auto build", "MAKE -j2", "net suke")),
    st.text(alphabet="xyzq", min_size=1, max_size=6),
)


def _cased_named_requests() -> st.SearchStrategy[str]:
    """Return each supported driver name in several casings and with padding."""
    return st.sampled_from(SUPPORTED_REQUESTS).flatmap(
        lambda name: st.sampled_from((
            name,
            name.upper(),
            name.title(),
            f"\t{name.upper()}  ",
        ))
    )


#: Supported requests in their variants, alongside arbitrary short text.
REQUESTED_DRIVERS = st.one_of(_cased_named_requests(), st.text(max_size=8))


@dataclasses.dataclass(slots=True, frozen=True)
class DriverScenario:
    """One controlled filesystem, tool environment, and driver request.

    Attributes
    ----------
    has_netsukefile
        Whether the repository holds a Netsukefile.
    has_makefile
        Whether the repository holds a Makefile.
    has_netsuke
        Whether the configured Netsuke executable resolves.
    has_make
        Whether the configured Make executable resolves.
    requested
        Raw ``build_driver`` option value handed to the selector.
    netsuke_bin
        Configured Netsuke executable name or path.
    make_bin
        Configured Make executable name or path.

    """

    has_netsukefile: bool
    has_makefile: bool
    has_netsuke: bool
    has_make: bool
    requested: str
    netsuke_bin: str
    make_bin: str

    @property
    def normalized_request(self) -> str:
        """The request as the selector compares it."""
        return self.requested.strip().lower()

    def manifests(self) -> tuple[str, ...]:
        """Return the manifest files this scenario places in the repository."""
        names: list[str] = []
        if self.has_netsukefile:
            names.append("Netsukefile")
        if self.has_makefile:
            names.append("Makefile")
        return tuple(names)

    def availability(self) -> dict[str, str | None]:
        """Return the path each configured executable resolves to, if any."""
        return {
            self.netsuke_bin: "/fake/bin/netsuke" if self.has_netsuke else None,
            self.make_bin: "/fake/bin/make" if self.has_make else None,
        }

    def options(self) -> state_mod.StopCheckOptions:
        """Return the runtime options that carry this scenario's request."""
        return state_mod.StopCheckOptions(
            always_fetch=False,
            max_out=MAX_OUT,
            build_driver=self.requested,
            netsuke_bin=self.netsuke_bin,
            make_bin=self.make_bin,
        )


@dataclasses.dataclass(slots=True, frozen=True)
class ControlledRepository:
    """A private repository whose manifests and tool lookup are fixed."""

    repo: Path
    options: state_mod.StopCheckOptions

    def select(self) -> driver_mod.BuildDriver | HookError | None:
        """Run one driver selection against the controlled environment."""
        return driver_mod.select_build_driver(self.repo, self.options)


def _which_from(
    available: cabc.Mapping[str, str | None],
) -> cabc.Callable[..., str | None]:
    """Build a ``shutil.which`` replacement that answers from a fixed mapping.

    The search path is deliberately ignored: a scenario decides availability,
    so an ambient `PATH` cannot change the answer between two calls.
    """

    def which(executable: str, *, path: str | None = None) -> str | None:
        """Return the recorded path for a tool, or ``None`` when unavailable."""
        return available.get(executable)

    return which


@contextlib.contextmanager
def controlled_repository(
    scenario: DriverScenario,
) -> cabc.Iterator[ControlledRepository]:
    """Create the scenario's manifests and pin tool availability for one example."""
    with tempfile.TemporaryDirectory() as raw_repo:
        repo = Path(raw_repo)
        for manifest in scenario.manifests():
            (repo / manifest).write_text("", encoding="utf-8")
        with mock.patch.object(shutil, "which", _which_from(scenario.availability())):
            yield ControlledRepository(repo=repo, options=scenario.options())


def _split(
    outcome: driver_mod.BuildDriver | HookError | None,
) -> tuple[driver_mod.BuildDriver | None, HookError | None]:
    """Separate a selection outcome into its driver and error halves.

    The final assertion states that the three shapes are exhaustive: any other
    value is a contract violation, not a silent skip, so the properties cannot
    mistake an unrecognised result for the documented "nothing to build" case.
    """
    if isinstance(outcome, driver_mod.BuildDriver):
        return outcome, None
    if isinstance(outcome, HookError):
        return None, outcome
    assert outcome is None, f"unexpected selection outcome: {outcome!r}"
    return None, None


@st.composite
def driver_scenarios(
    draw: st.DrawFn,
    *,
    requested: st.SearchStrategy[str] | None = None,
) -> DriverScenario:
    """Draw one complete scenario, optionally restricting the request."""
    return DriverScenario(
        has_netsukefile=draw(st.booleans()),
        has_makefile=draw(st.booleans()),
        has_netsuke=draw(st.booleans()),
        has_make=draw(st.booleans()),
        requested=draw(REQUESTED_DRIVERS if requested is None else requested),
        netsuke_bin=draw(st.sampled_from(NETSUKE_BINARIES)),
        make_bin=draw(st.sampled_from(MAKE_BINARIES)),
    )


@given(driver_scenarios())
def test_selection_is_deterministic_and_returns_one_shape(
    scenario: DriverScenario,
) -> None:
    """The same inputs always produce the same single outcome shape."""
    with controlled_repository(scenario) as environment:
        first = environment.select()
        second = environment.select()

    assert first == second, f"expected {first!r} twice but got {second!r}"
    driver, error = _split(first)
    assert driver is None or error is None, (
        f"expected a driver or an error, never both, but got {first!r}"
    )
    if driver is not None:
        assert driver.name in SUPPORTED_DRIVER_NAMES, (
            f"expected a supported driver name but got {driver.name!r}"
        )
    if error is not None:
        assert error.operation == "select_build_driver", (
            f"expected the selection operation but got {error.operation!r}"
        )
        assert str(error).strip(), f"expected an explained failure but got {error!r}"


@given(driver_scenarios())
def test_a_selected_driver_is_runnable(scenario: DriverScenario) -> None:
    """A returned driver's manifest exists here and its tool resolves here."""
    with controlled_repository(scenario) as environment:
        driver, _error = _split(environment.select())
        if driver is None:
            return
        configured_bin = (
            scenario.netsuke_bin if driver.name == "netsuke" else scenario.make_bin
        )
        assert (environment.repo / driver.manifest).is_file(), (
            f"expected {driver.manifest} to exist for {driver.name}"
        )
        assert driver.executable == configured_bin, (
            f"expected the configured {configured_bin!r} but got {driver.executable!r}"
        )
        assert scenario.availability()[driver.executable] is not None, (
            f"expected {driver.executable!r} to resolve in this scenario"
        )


@given(driver_scenarios(requested=st.sampled_from(("make", "netsuke"))))
def test_an_explicit_request_never_selects_the_other_driver(
    scenario: DriverScenario,
) -> None:
    """Asking for make cannot produce netsuke, and the reverse holds too."""
    with controlled_repository(scenario) as environment:
        driver, _error = _split(environment.select())

    if driver is not None:
        assert driver.name == scenario.requested, (
            f"expected the requested {scenario.requested!r} but got {driver.name!r}"
        )


@given(driver_scenarios(requested=st.sampled_from(("make", "netsuke"))))
def test_an_explicit_request_succeeds_only_with_manifest_and_tool(
    scenario: DriverScenario,
) -> None:
    """A forced driver is returned exactly when both of its prerequisites hold."""
    manifest, has_manifest, has_tool = {
        "make": ("Makefile", scenario.has_makefile, scenario.has_make),
        "netsuke": ("Netsukefile", scenario.has_netsukefile, scenario.has_netsuke),
    }[scenario.requested]

    with controlled_repository(scenario) as environment:
        driver, error = _split(environment.select())

    assert (driver is not None) == (has_manifest and has_tool)
    if driver is not None:
        return
    assert error is not None, "a failed explicit request must explain itself"
    if not has_manifest:
        assert f"{manifest} is missing" in str(error), (
            f"expected a missing-manifest explanation but got {str(error)!r}"
        )
    else:
        assert not has_tool
        configured_bin = (
            scenario.netsuke_bin
            if scenario.requested == "netsuke"
            else scenario.make_bin
        )
        assert f"{configured_bin} not found" in str(error), (
            f"expected a missing-executable explanation but got {str(error)!r}"
        )


@given(driver_scenarios())
def test_selection_is_skipped_only_when_no_manifest_exists(
    scenario: DriverScenario,
) -> None:
    """Automatic selection skips exactly when neither supported manifest exists."""
    with controlled_repository(scenario) as environment:
        outcome = environment.select()

    is_automatic = scenario.normalized_request == "auto"
    has_manifest = scenario.has_netsukefile or scenario.has_makefile
    assert (outcome is None) == (is_automatic and not has_manifest), (
        f"expected a skip only for an automatic request with no manifest, but got "
        f"{outcome!r} for {scenario!r}"
    )


@given(driver_scenarios(requested=UNSUPPORTED_REQUESTS))
def test_an_unsupported_request_is_rejected(scenario: DriverScenario) -> None:
    """A request outside the supported names is refused with an explanation."""
    with controlled_repository(scenario) as environment:
        outcome = environment.select()

    assert isinstance(outcome, HookError), (
        f"expected an unsupported-driver error but got {outcome!r}"
    )
    assert str(outcome).startswith("Unsupported build driver"), (
        f"expected the unsupported-driver explanation but got {str(outcome)!r}"
    )
