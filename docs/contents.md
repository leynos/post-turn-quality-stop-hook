# Documentation contents

This index lists the long-lived documentation for post-turn-quality-stop-hook.

## Documentation index

- [Developers' guide](developers-guide.md) - local development, quality gates,
  test strategy, and tool bootstrapping.
- [Users' guide](users-guide.md) - hook installation, configuration, and
  runtime behaviour.
- [Documentation style guide](documentation-style-guide.md) - prose, Markdown,
  and architectural decision record conventions.
- [Scripting standards](scripting-standards.md) - helper-script design and
  command-execution expectations.

## Migration notes

- [Migrating to 0.2.0](migrations/0.2.0.md) - quality-gate configuration,
  plain captured output, and escape-sequence removal.

## Design and decisions

- [ADR-001: Four-tier Python linting](adrs/adr-0001-four-tier-python-linting.md)
  - accepted decision for the four distinct tiers: Ruff formatting, Ruff static
  linting, `ty` type checking, and Skylos production dead-code detection.
- [ADR-002: Verify the unpushed upstream head](adrs/adr-0002-verify-unpushed-upstream.md)
  - accepted decision to refresh the configured upstream into a private ref
  before comparing `HEAD`, failing open when the remote cannot be verified.
