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
    fresh_documents,
    job_steps,
    lane_jobs,
)
from codescene_pull_request_rules import pull_request_closure, pull_request_contacts
from codescene_workflow_reader import (
    Document,
    WorkflowError,
    load_workflow,
    read_workflows,
)

if typ.TYPE_CHECKING:
    from pathlib import Path

UPLOADER = "leynos/shared-actions/.github/actions/upload-codescene-coverage"

pytestmark = pytest.mark.skipif(not WORKFLOWS.is_dir(), reason=SKIP_REASON)


@pytest.fixture
def documents() -> Documents:
    """Give each test its own copy of the workflows to mutate."""
    return fresh_documents()


def test_repository_keeps_codescene_off_pull_requests(documents: Documents) -> None:
    """Hold every pull-request clause over the workflows as committed."""
    assert pull_request_contacts(documents) == []


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
    assert PROBE in pull_request_closure(documents)
    found = pull_request_contacts(documents)
    assert f"{PROBE} names the CodeScene host" in found
    assert f"{PROBE} puts CS_ACCESS_TOKEN in reach" in found
    assert f"{LANE} job probe forwards every secret with `secrets: inherit`" in found


def test_closure_follows_a_workflow_run_chain(documents: Documents) -> None:
    """A workflow_run chained onto a pull-request workflow is a PR lane."""
    _, callee = _probe()
    lane = str(documents[LANE].get("name", LANE))
    callee[True] = {"workflow_run": {"workflows": [lane]}}
    documents[PROBE] = callee
    assert f"{PROBE} names the CodeScene host" in pull_request_contacts(documents)


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
    assert f"{PROBE} names the CodeScene host" in pull_request_contacts(documents)


@pytest.mark.parametrize(
    "event", ["merge_group", "pull_request_review", "pull_request_review_comment"]
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
    assert f"{PROBE} names the CodeScene host" in pull_request_contacts(documents)


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
    assert f"{PROBE} puts CS_ACCESS_TOKEN in reach" in pull_request_contacts(documents)


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
    assert f"{LANE} {reason}" in pull_request_contacts(documents)


def test_token_in_workflow_env_is_refused(documents: Documents) -> None:
    """A workflow-level env reaches every step of every job."""
    documents[LANE]["env"] = {"CS_ACCESS_TOKEN": CREDENTIAL_REFERENCE}
    assert f"{LANE} puts CS_ACCESS_TOKEN in reach" in pull_request_contacts(documents)


def test_host_in_workflow_defaults_is_refused(documents: Documents) -> None:
    """A default shell runs before every step, naming no step at all."""
    documents[LANE]["defaults"] = {
        "run": {"shell": "curl -s https://Api.CodeScene.io >/dev/null; bash {0}"}
    }
    assert f"{LANE} names the CodeScene host" in pull_request_contacts(documents)


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
    assert f"{PROBE} names the cs-coverage client" in pull_request_contacts(documents)


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
    assert sorted(read_workflows(tmp_path)) == ["a.YML", "b.yaml"]


def test_reader_names_the_file_for_invalid_yaml() -> None:
    """A parser error must say which workflow it came from."""
    with pytest.raises(WorkflowError, match=r"^bad\.yml: not valid YAML"):
        load_workflow("bad.yml", "jobs: [\n")
