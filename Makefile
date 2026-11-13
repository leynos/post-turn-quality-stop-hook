MDLINT ?= markdownlint-cli2
# `make fmt` and `make check-fmt` call mdtablefix directly. `--git` selects the
# Markdown files Git tracks and `--include-untracked` adds the untracked files
# Git does not ignore, so a new document is formatted before it is staged.
# Both modes need mdtablefix 0.6.0 or later; CI pins the version at the
# install-mdtablefix step.
MDTABLEFIX ?= mdtablefix
MDTABLEFIX_SELECT = --git --include-untracked
MDTABLEFIX_RULES = --wrap --renumber --breaks --ellipsis --fences
NIXIE ?= nixie
TOOLS = $(MDLINT) uv
VENV_TOOLS = pytest ruff ty mbake

# Every uv call goes through the vendored helper, which cleans the environment,
# uses the global uv cache, runs offline first and goes online at most once. See
# https://github.com/leynos/shared-actions/tree/main/uv_gate for the contract.
UV_GATE ?= python3 scripts/uv_gate.py
UV ?= uv
# The helper finds uv on PATH, so a caller's UV=/path/to/uv is honoured by putting
# that uv's directory first on PATH for every recipe.
export PATH := $(if $(findstring /,$(UV)),$(patsubst %/,%,$(dir $(UV))):)$(PATH)
# The CV-005 CodeScene contracts live in shared-actions and run from a full
# commit, so a fix is a pin bump. `.github/cv005.toml` holds this repository's
# only parameters.
CV005_CONTRACTS_REF ?= 88977798a5c3bae1549afb99642529488c665276
CV005_CONTRACTS = $(UV_GATE) tool --python 3.13 --from 'git+https://github.com/leynos/shared-actions@$(CV005_CONTRACTS_REF)\#subdirectory=packages/cv005-contracts' -- cv005-contracts

PYTHON_TARGETS ?= post_turn_quality_stop_hook tests
# The dead-code gate is the installed df12-skylos command; its `skylos` extra
# carries the one Skylos pin. Skylos parses source with its own runtime AST, so
# the tool environment runs Python 3.14.
DF12_PYTHON_LINTS_SHA = 56ad71721c3f1f2164ec84b69150582488f1a927
SKYLOS = $(UV_GATE) tool --python 3.14 --from \
  'df12-python-lints[skylos] @ git+https://github.com/leynos/df12-python-lints.git@$(DF12_PYTHON_LINTS_SHA)' \
  -- df12-skylos

.PHONY: prepare help all clean build build-release lint fmt check-fmt \
        markdownlint spelling nixie test typecheck validate-makefile \
        skylos skylos-allow $(TOOLS) $(VENV_TOOLS) test-workflow-contracts

prepare: ## Install the locked development environment, offline first
	$(UV_GATE) prepare --group dev

.DEFAULT_GOAL := all

test-workflow-contracts: ## Check the CV-005 CodeScene workflow contracts
	$(CV005_CONTRACTS) check --repository .

all: build validate-makefile check-fmt lint typecheck test spelling test-workflow-contracts

build: uv prepare ## Build virtual-env and install deps

build-release: ## Build artefacts (sdist & wheel)
	python -m build --sdist --wheel

clean: ## Remove build artifacts
	rm -rf build dist *.egg-info \
	  .mypy_cache .pytest_cache .coverage coverage.* \
	  lcov.info htmlcov .venv
	find . -type d -name '__pycache__' -print0 | xargs -0 -r rm -rf
	rm -f .typos-oxendict-base.json .typos-oxendict-base.toml

define ensure_tool
	@command -v $(1) >/dev/null 2>&1 || { \
	  printf "Error: '%s' is required, but not installed\n" "$(1)" >&2; \
	  exit 1; \
	}
endef

define ensure_tool_venv
	@$(UV_GATE) run -- which $(1) >/dev/null 2>&1 || { \
	  printf "Error: '%s' is required in the virtualenv, but is not installed\n" "$(1)" >&2; \
	  exit 1; \
	}
endef

ifneq ($(strip $(TOOLS)),)
$(TOOLS): ## Verify required CLI tools
	$(call ensure_tool,$@)
endif


ifneq ($(strip $(VENV_TOOLS)),)
.PHONY: $(VENV_TOOLS)
$(VENV_TOOLS): prepare ## Verify required CLI tools in venv
	$(call ensure_tool_venv,$@)
endif

fmt: build ruff ## Format sources
	$(UV_GATE) run -- ruff format
	$(UV_GATE) run -- ruff check --select I --fix
	$(MDTABLEFIX) --in-place $(MDTABLEFIX_SELECT) $(MDTABLEFIX_RULES)
	$(MDLINT) --fix "**/*.md"

check-fmt: build ruff ## Verify formatting
	$(UV_GATE) run -- ruff format --check
	$(MDTABLEFIX) --check $(MDTABLEFIX_SELECT) $(MDTABLEFIX_RULES)

lint: build ruff ## Run linters
	$(UV_GATE) run -- ruff check
	$(MAKE) --no-print-directory skylos

skylos: ## Detect dead production code
	$(SKYLOS) check --repository .

# Interrogate scans each target recursively, subject to its default exclusions.
INTERROGATE_VERSION ?= 1.7.0
INTERROGATE_TARGETS ?= $(PYTHON_TARGETS) scripts
# Level 2 prints a MISSED row per undocumented definition, which the
# `interrogate` recipe below looks for: the rounded percentage alone can pass a
# lone gap.
INTERROGATE_FLAGS ?= -vv --fail-under 100
INTERROGATE ?= $(UV_GATE) tool --python 3.14 --from 'interrogate==$(INTERROGATE_VERSION)' -- interrogate

# Interrogate rounds its percentage to one decimal, so `--fail-under 100` alone
# can pass a lone gap. This recipe also fails when `grep` finds a MISSED row in
# the report on standard output. `grep` reads the whole report (no `-q`), so an
# early exit cannot break the pipe under `pipefail`.
.PHONY: interrogate
interrogate: ## Enforce complete Python docstring coverage
	@report="$$($(INTERROGATE) $(INTERROGATE_FLAGS) $(INTERROGATE_TARGETS))"; \
	  status=$$?; \
	  printf '%s\n' "$$report"; \
	  test "$$status" -eq 0 || exit "$$status"; \
	  if printf '%s\n' "$$report" | grep '| *MISSED *|' >/dev/null; then \
	    printf '%s\n' 'interrogate: undocumented definitions remain; see the MISSED rows above' >&2; \
	    exit 1; \
	  fi

# `lint` runs the docstring gate; Make adds this prerequisite to the `lint`
# rule above.
lint: interrogate

skylos-allow: export SKYLOS_SYMBOL = $(value SYMBOL)
skylos-allow: export SKYLOS_REASON = $(value REASON)
skylos-allow: ## Document one named Skylos exception (SYMBOL=name REASON=text)
	$(SKYLOS) allow --repository . --symbol="$${SKYLOS_SYMBOL}" --reason="$${SKYLOS_REASON}"

typecheck: build ty ## Run typechecking
	$(UV_GATE) run -- ty --version
	$(UV_GATE) run -- ty check $(PYTHON_TARGETS)

validate-makefile: build mbake ## Validate Makefile syntax and structure
	$(UV_GATE) run -- mbake validate Makefile

markdownlint: $(MDLINT) ## Lint Markdown files
	$(MDLINT) '**/*.md'
	+$(MAKE) spelling

TYPOS_VERSION ?= 1.48.0
TYPOS := $(UV_GATE) tool --from typos@$(TYPOS_VERSION) -- typos

spelling: prepare ## Enforce en-GB-oxendict spelling in Markdown prose
	$(UV_GATE) run -- python scripts/generate_typos_config.py
	find . -type f -name '*.md' -not -path './.venv/*' -not -path './.uv-cache/*' -not -path './.uv-tools/*' -print0 | \
		xargs -0 -r $(TYPOS) --config typos.toml --force-exclude

nixie: ## Validate Mermaid diagrams
	$(call ensure_tool,nixie)
	$(NIXIE) --no-sandbox

test: build uv $(VENV_TOOLS) ## Run tests
	$(UV_GATE) run -- pytest -v -n auto

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?##' $(MAKEFILE_LIST) | \
	awk 'BEGIN {FS=":"; printf "Available targets:\n"} {printf "  %-20s %s\n", $$1, $$2}'
