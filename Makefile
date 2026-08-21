MDLINT ?= markdownlint-cli2
NIXIE ?= nixie
MDFORMAT_ALL ?= mdformat-all
TOOLS = $(MDFORMAT_ALL) $(MDLINT) uv
VENV_TOOLS = pytest ruff ty mbake
UV_ENV = UV_CACHE_DIR=.uv-cache UV_TOOL_DIR=.uv-tools
PYTHON_TARGETS ?= post_turn_quality_stop_hook tests
SKYLOS_VERSION = 4.33.2
SKYLOS = $(UV_ENV) uv tool run --from 'skylos==$(SKYLOS_VERSION)' skylos \
	--config-file pyproject.toml
SKYLOS_PRODUCTION_TARGETS ?= post_turn_quality_stop_hook

.PHONY: help all clean build build-release lint fmt check-fmt \
	markdownlint spelling nixie test typecheck validate-makefile skylos-allow \
	$(TOOLS) $(VENV_TOOLS)

.DEFAULT_GOAL := all

all: build validate-makefile check-fmt lint typecheck test spelling

.venv: pyproject.toml
	$(UV_ENV) uv venv --clear

build: uv .venv ## Build virtual-env and install deps
	$(UV_ENV) uv sync --group dev

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
	@$(UV_ENV) uv run which $(1) >/dev/null 2>&1 || { \
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
$(VENV_TOOLS): ## Verify required CLI tools in venv
	$(call ensure_tool_venv,$@)
endif

fmt: build ruff $(MDFORMAT_ALL) ## Format sources
	$(UV_ENV) uv run ruff format
	$(UV_ENV) uv run ruff check --select I --fix
	$(MDFORMAT_ALL)

check-fmt: build ruff ## Verify formatting
	$(UV_ENV) uv run ruff format --check
	# mdformat-all doesn't currently do checking

lint: build ruff ## Run linters
	$(UV_ENV) uv run ruff check
	$(SKYLOS) $(SKYLOS_PRODUCTION_TARGETS) --category dead_code --gate \
		--format concise --no-upload --no-provenance --no-grep-verify

skylos-allow: export SKYLOS_NAME = $(value NAME)
skylos-allow: export SKYLOS_REASON = $(value REASON)
skylos-allow: ## Document one named Skylos false positive
	@test -n "$${SKYLOS_NAME}" || { printf "Error: NAME is required for a named Skylos exception\\n" >&2; exit 2; }
	@test -n "$${SKYLOS_REASON}" || { printf "Error: REASON is required for a named Skylos exception\\n" >&2; exit 2; }
	$(SKYLOS) whitelist "$${SKYLOS_NAME}" --reason "$${SKYLOS_REASON}"

typecheck: build ty ## Run typechecking
	$(UV_ENV) uv run ty --version
	$(UV_ENV) uv run ty check $(PYTHON_TARGETS)

validate-makefile: build mbake ## Validate Makefile syntax and structure
	$(UV_ENV) uv run mbake validate Makefile

markdownlint: $(MDLINT) ## Lint Markdown files
	$(MDLINT) '**/*.md'
	+$(MAKE) spelling

TYPOS_VERSION ?= 1.48.0
TYPOS := uv tool run typos@$(TYPOS_VERSION)

spelling: ## Enforce en-GB-oxendict spelling in Markdown prose
	uv run scripts/generate_typos_config.py
	find . -type f -name '*.md' -not -path './.venv/*' -print0 | \
		xargs -0 -r $(TYPOS) --config typos.toml --force-exclude

nixie: ## Validate Mermaid diagrams
	$(call ensure_tool,nixie)
	$(NIXIE) --no-sandbox

test: build uv $(VENV_TOOLS) ## Run tests
	$(UV_ENV) uv run pytest -v -n auto

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?##' $(MAKEFILE_LIST) | \
	awk 'BEGIN {FS=":"; printf "Available targets:\n"} {printf "  %-20s %s\n", $$1, $$2}'
