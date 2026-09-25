"""Prove the pull-request half of CV-005: no lane a PR starts reaches CodeScene.

The first test judges this repository's workflows. Every other test mutates a
copy of them in the way a later edit could, or feeds the reader a document it
must refuse, and asserts that the clause meant to catch that edit does. A clause
no mutation fails is indistinguishable from one never written.
"""

from __future__ import annotations

import typing as typ

import pytest
from codescene_contract_support import (
    CREDENTIAL_REFERENCE,
    LANE,
    PROBE,
    SKIP_REASON,
    WORKFLOWS,
    Documents,
    fresh_actions,
    job_steps,
    lane_jobs,
)
from codescene_pull_request_rules import (
    REPOSITORY,
    pull_request_closure,
    pull_request_contacts,
)
from codescene_workflow_files import read_actions, read_workflows
from codescene_workflow_reader import Document, WorkflowError, load_workflow

if typ.TYPE_CHECKING:
    from pathlib import Path

UPLOADER = "leynos/shared-actions/.github/actions/upload-codescene-coverage"
ACTION = ".github/actions/probe"
LEAK = "      - run: curl https://api.codescene.io/v2\n        shell: bash\n"

pytestmark = pytest.mark.skipif(not WORKFLOWS.is_dir(), reason=SKIP_REASON)


def _contacts(documents: Documents, actions: Documents | None = None) -> list[str]:
    """Run the pull-request rule over the workflows and the local actions."""
    return pull_request_contacts(
        documents, fresh_actions() if actions is None else actions
    )


def test_repository_keeps_codescene_off_pull_requests(documents: Documents) -> None:
    """Hold every pull-request clause over the workflows as committed."""
    found = _contacts(documents)
    assert found == [], f"pull-request lanes reach CodeScene: {found}"


def _assert_contact(documents: Documents, expected: str) -> None:
    """Assert that the pull-request rule reports one expected contact."""
    found = _contacts(documents)
    assert expected in found, f"missing {expected!r} in {found}"


def _probe(uses_prefix: str = "./") -> tuple[Document, Document]:
    """Return a pull-request caller job and a callee reaching CodeScene.

    The callee declares only `workflow_call`, so it serves no pull request by
    its own trigger, and receives the token through `secrets: inherit`.
    """
    callee = load_workflow(
        PROBE,
        "on:\n  workflow_call:\njobs:\n  leak:\n    runs-on: ubuntu-latest\n"
        '    steps:\n      - run: curl -H "$T" https://api.codescene.io/v2\n'
        f"        env:\n          T: '{CREDENTIAL_REFERENCE}'\n",
    )
    caller = {"uses": f"{uses_prefix}.github/workflows/{PROBE}", "secrets": "inherit"}
    return caller, callee


@pytest.mark.parametrize("prefix", ["./", "$/"])
def test_closure_follows_a_called_workflow(documents: Documents, prefix: str) -> None:
    """A workflow_call callee of a pull-request job is judged as a PR lane."""
    caller, callee = _probe(prefix)
    documents[PROBE] = callee
    lane_jobs(documents)["probe"] = caller
    assert PROBE in pull_request_closure(documents), "the callee left the closure"
    found = _contacts(documents)
    for expected in (
        f"{PROBE} names the CodeScene host",
        f"{PROBE} puts CS_ACCESS_TOKEN in reach",
        f"{LANE} job probe forwards every secret with `secrets: inherit`",
    ):
        assert expected in found, f"missing {expected!r} in {found}"


def _composite(*steps: str) -> Document:
    """Return a composite action running the given step lines."""
    body = "".join(steps)
    return load_workflow(
        f"{ACTION}/action.yml",
        f"name: probe\nruns:\n  using: composite\n  steps:\n{body}",
    )


@pytest.mark.parametrize("prefix", ["./", "$/"])
def test_closure_scans_a_local_action(documents: Documents, prefix: str) -> None:
    """A local composite action a pull-request step runs is judged as a lane."""
    actions = fresh_actions()
    actions[ACTION] = _composite(LEAK)
    job_steps(documents[LANE]).append({"uses": f"{prefix}{ACTION}"})
    found = _contacts(documents, actions)
    assert f"{ACTION} names the CodeScene host" in found, f"missed in {found}"


def test_closure_follows_a_nested_local_action(documents: Documents) -> None:
    """A local action run by a reached local action is judged as well."""
    actions = fresh_actions()
    actions[ACTION] = _composite("      - uses: ./.github/actions/inner/\n")
    actions[".github/actions/inner"] = _composite(LEAK)
    job_steps(documents[LANE]).append({"uses": f"./{ACTION}"})
    found = _contacts(documents, actions)
    expected = ".github/actions/inner names the CodeScene host"
    assert expected in found, f"missing {expected!r} in {found}"


def test_unreached_local_action_stays_off_the_surface(documents: Documents) -> None:
    """The action rule is narrow: an action no PR step runs is not judged."""
    actions = fresh_actions()
    actions[ACTION] = _composite(LEAK)
    assert _contacts(documents, actions) == [], "an unreached action was judged"


@pytest.mark.parametrize(
    ("uses", "reason"),
    [
        (f"$/{ACTION}@main", "a `\\$/` call cannot name a ref"),
        (f"{REPOSITORY}/{ACTION}@main", "runs this repository's action at a ref"),
        ("./actions/missing", "names no action under .github in this repository"),
    ],
)
def test_closure_refuses_actions_it_cannot_read(
    documents: Documents, uses: str, reason: str
) -> None:
    """A local action the closure cannot follow to a checked-out file is refused."""
    job_steps(documents[LANE]).append({"uses": uses})
    with pytest.raises(WorkflowError, match=reason):
        _contacts(documents)


def test_reader_reads_every_local_action(tmp_path: Path) -> None:
    """Actions are keyed by directory, at any depth under `.github`."""
    for directory in ("actions/a", "actions/deep/b"):
        (tmp_path / ".github" / directory).mkdir(parents=True)
    (tmp_path / ".github/actions/a/action.yml").write_text("runs: {}\n")
    (tmp_path / ".github/actions/deep/b/action.yaml").write_text("runs: {}\n")
    found = sorted(read_actions(tmp_path))
    expected = [".github/actions/a", ".github/actions/deep/b"]
    assert found == expected, f"actions read: {found}"


def test_reader_refuses_an_action_declared_twice(tmp_path: Path) -> None:
    """GitHub reads one metadata file; a reader of either could be misled."""
    directory = tmp_path / ".github/actions/a"
    directory.mkdir(parents=True)
    for name in ("action.yml", "action.yaml"):
        (directory / name).write_text("runs: {}\n")
    with pytest.raises(WorkflowError, match="declares both"):
        read_actions(tmp_path)


def test_closure_follows_a_workflow_run_chain(documents: Documents) -> None:
    """A workflow_run chained onto a pull-request workflow is a PR lane."""
    _, callee = _probe()
    lane = str(documents[LANE].get("name", LANE))
    callee[True] = {"workflow_run": {"workflows": [lane]}}
    documents[PROBE] = callee
    _assert_contact(documents, f"{PROBE} names the CodeScene host")


def test_workflow_run_matches_an_unnamed_workflow_by_path(
    documents: Documents,
) -> None:
    """GitHub names a workflow without `name:` by its path from the root."""
    _, callee = _probe()
    documents[LANE].pop("name", None)
    callee[True] = {"workflow_run": {"workflows": [f".github/workflows/{LANE}"]}}
    documents[PROBE] = callee
    _assert_contact(documents, f"{PROBE} names the CodeScene host")


@pytest.mark.parametrize(
    ("uses", "reason"),
    [
        ("$/.github/workflows/ci.yml@main", "a `\\$/` call cannot name a ref"),
        (
            "leynos/post-turn-quality-stop-hook/.github/workflows/ci.yml@main",
            "runs this repository's workflow at a ref",
        ),
        ("./.github/workflows/missing.yml", "names no workflow in this repository"),
    ],
)
def test_closure_refuses_calls_it_cannot_read(
    documents: Documents, uses: str, reason: str
) -> None:
    """A call the closure cannot follow to a checked-out file is refused."""
    lane_jobs(documents)["probe"] = {"uses": uses}
    with pytest.raises(WorkflowError, match=reason):
        pull_request_closure(documents)


def test_closure_starts_from_pull_request_target(documents: Documents) -> None:
    """A pull_request_target workflow runs with secrets on every PR event."""
    documents[PROBE] = load_workflow(
        PROBE,
        "on: pull_request_target\njobs:\n  a:\n    steps:\n"
        "      - run: curl https://codescene.io\n",
    )
    _assert_contact(documents, f"{PROBE} names the CodeScene host")


@pytest.mark.parametrize(
    "event",
    [
        "issue_comment",
        "merge_group",
        "pull_request_review",
        "pull_request_review_comment",
        "push",
        "{push: {branches: ['**']}}",
    ],
)
def test_closure_starts_from_every_pull_request_event(
    documents: Documents, event: str
) -> None:
    """A queued merge or a review runs with secrets for a same-repository PR."""
    documents[PROBE] = load_workflow(
        PROBE,
        f"on: {event}\njobs:\n  a:\n    steps:\n"
        "      - run: curl https://codescene.io\n",
    )
    _assert_contact(documents, f"{PROBE} names the CodeScene host")


@pytest.mark.parametrize(
    "trigger",
    [
        "{push: {branches: [main]}}",
        "{push: {tags: ['v*']}}",
        "{schedule: [{cron: '0 0 * * *'}]}",
    ],
)
def test_trunk_tag_and_schedule_triggers_stay_off_the_surface(
    documents: Documents, trigger: str
) -> None:
    """The seed rule is narrow: these runs never start for a pull request."""
    documents[PROBE] = load_workflow(
        PROBE,
        f"on: {trigger}\njobs:\n  a:\n    steps:\n"
        "      - run: curl https://codescene.io\n",
    )
    closure = pull_request_closure(documents)
    assert PROBE not in closure, f"{trigger} reached the pull-request surface"


def test_callee_secret_declaration_is_refused(documents: Documents) -> None:
    """A called workflow declaring the secret by name is refused.

    The declaration is a mapping key with no reference in any value, so only
    a reading of keys as well as values sees it.
    """
    documents[PROBE] = load_workflow(
        PROBE,
        "on:\n  workflow_call:\n    secrets:\n      CS_ACCESS_TOKEN:\n"
        "        required: false\njobs: {}\n",
    )
    lane_jobs(documents)["probe"] = {"uses": f"./.github/workflows/{PROBE}"}
    _assert_contact(documents, f"{PROBE} puts CS_ACCESS_TOKEN in reach")


@pytest.mark.parametrize(
    ("step", "reason"),
    [
        ({"run": f"echo {CREDENTIAL_REFERENCE}"}, "puts CS_ACCESS_TOKEN in reach"),
        (
            {"uses": "x/y@v1", "with": {"t": CREDENTIAL_REFERENCE}},
            "puts CS_ACCESS_TOKEN in reach",
        ),
        (
            {"run": "true", "env": {"OTHER": CREDENTIAL_REFERENCE}},
            "puts CS_ACCESS_TOKEN in reach",
        ),
        ({"run": "echo '${{ toJSON( secrets ) }}'"}, "serializes the secrets context"),
        ({"run": "echo ${{ secrets['CS_' + 'X'] }}"}, "indexes the secrets context"),
        ({"run": "curl https://API.CODESCENE.IO"}, "names the CodeScene host"),
        ({"run": "cs-coverage check coverage.xml"}, "names the cs-coverage client"),
        (
            {"uses": f"{UPLOADER}@x"},
            "calls the CodeScene uploader",
        ),
    ],
)
def test_pull_request_lane_cannot_reach_codescene(
    documents: Documents, step: dict[str, object], reason: str
) -> None:
    """Every route to CodeScene or its token from a PR step is refused."""
    job_steps(documents[LANE]).append(step)
    _assert_contact(documents, f"{LANE} {reason}")


def test_token_in_workflow_env_is_refused(documents: Documents) -> None:
    """A workflow-level env reaches every step of every job."""
    documents[LANE]["env"] = {"CS_ACCESS_TOKEN": CREDENTIAL_REFERENCE}
    _assert_contact(documents, f"{LANE} puts CS_ACCESS_TOKEN in reach")


def test_host_in_workflow_defaults_is_refused(documents: Documents) -> None:
    """A default shell runs before every step, naming no step at all."""
    documents[LANE]["defaults"] = {
        "run": {"shell": "curl -s https://Api.CodeScene.io >/dev/null; bash {0}"}
    }
    _assert_contact(documents, f"{LANE} names the CodeScene host")


def test_duplicate_keys_are_refused() -> None:
    """PyYAML would keep the second `runs-on` and discard the first."""
    text = "on: push\njobs:\n  a:\n    runs-on: x\n    runs-on: y\n    steps: []\n"
    with pytest.raises(WorkflowError, match="duplicate key 'runs-on'"):
        load_workflow("dup.yml", text)


@pytest.mark.parametrize(
    "trigger",
    ["on: pull_request", "on: [push, pull_request]", "'on': {pull_request: {}}"],
)
def test_every_trigger_form_is_read(documents: Documents, trigger: str) -> None:
    """Scalar, sequence and quoted-key mapping triggers all serve PRs."""
    documents[PROBE] = load_workflow(
        PROBE, f"{trigger}\njobs:\n  a:\n    steps:\n      - run: cs-coverage check\n"
    )
    _assert_contact(documents, f"{PROBE} names the cs-coverage client")


def test_both_trigger_spellings_are_refused() -> None:
    """GitHub merges `on` and `'on'`; a reader of either is blind to the other."""
    text = "on: push\n'on': pull_request\njobs: {}\n"
    with pytest.raises(WorkflowError, match="declares `on` 2 times"):
        pull_request_closure({"both.yml": load_workflow("both.yml", text)})


def test_reader_refuses_an_empty_directory(tmp_path: Path) -> None:
    """Finding no workflow is the reader failing, not the repository passing."""
    with pytest.raises(WorkflowError, match="no workflows were read"):
        read_workflows(tmp_path)


def test_reader_reads_every_suffix_and_case(tmp_path: Path) -> None:
    """GitHub runs `.yaml` and upper-case suffixes too."""
    for name in ("a.YML", "b.yaml"):
        (tmp_path / name).write_text("on: push\njobs: {}\n", encoding="utf-8")
    found = sorted(read_workflows(tmp_path))
    assert found == ["a.YML", "b.yaml"], f"workflows read: {found}"


def test_reader_refuses_a_missing_directory(tmp_path: Path) -> None:
    """A directory that cannot be listed fails at the reader, naming it."""
    with pytest.raises(WorkflowError, match="cannot list workflows"):
        read_workflows(tmp_path / "missing")


def test_reader_refuses_a_file_that_is_not_utf8(tmp_path: Path) -> None:
    """Undecodable bytes fail at the reader, naming the file."""
    (tmp_path / "bad.yml").write_bytes(b"\xff\xfe")
    with pytest.raises(WorkflowError, match=r"^bad\.yml: cannot be read as UTF-8"):
        read_workflows(tmp_path)


def test_reader_names_the_file_for_invalid_yaml() -> None:
    """A parser error must say which workflow it came from."""
    with pytest.raises(WorkflowError, match=r"^bad\.yml: not valid YAML"):
        load_workflow("bad.yml", "jobs: [\n")
