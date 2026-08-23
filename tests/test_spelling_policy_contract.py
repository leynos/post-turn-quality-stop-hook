"""Regression tests for the committed repository spelling policy."""

from __future__ import annotations

import importlib
import tomllib
import typing as typ
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIRECTORY = REPOSITORY_ROOT / "scripts"
LOCAL_POLICY_PATH = REPOSITORY_ROOT / "typos.local.toml"


@pytest.fixture(name="spelling_modules")
def spelling_modules_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[typ.Any, typ.Any]:
    """Import the spelling scripts through their runtime module path."""
    monkeypatch.syspath_prepend(str(SCRIPTS_DIRECTORY))
    importlib.invalidate_caches()
    return (
        importlib.import_module("typos_rollout"),
        importlib.import_module("generate_typos_config"),
    )


def test_committed_local_policy_survives_configuration_generation(
    spelling_modules: tuple[typ.Any, typ.Any],
    tmp_path: Path,
) -> None:
    """Generation must preserve the reviewed, committed repository policy."""
    _rollout, generator = spelling_modules
    (tmp_path / ".typos-oxendict-base.toml").write_text(
        'schema = 1\n\n[oxford]\nstems = ["organ"]\n\n[words]\naccepted = []\n\n'
        "[words.corrections]\n\n[patterns]\nignore = []\n\n[files]\nexclude = []\n",
        encoding="utf-8",
    )
    committed_policy = LOCAL_POLICY_PATH.read_text(encoding="utf-8")
    (tmp_path / "typos.local.toml").write_text(committed_policy, encoding="utf-8")

    expected_policy = tomllib.loads(committed_policy)
    generated = tomllib.loads(generator.render_config(tmp_path))
    expected_words = expected_policy["words"]["accepted"]
    generated_words = generated["default"]["extend-words"]

    assert all(word in generated_words for word in expected_words), (
        "Generated spelling configuration must retain every committed accepted word."
    )
