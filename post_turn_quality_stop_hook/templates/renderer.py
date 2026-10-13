"""Render bundled Jinja templates."""

from __future__ import annotations

from importlib import resources

from jinja2 import Environment, select_autoescape

_ENV = Environment(
    autoescape=select_autoescape(default_for_string=False, default=False),
    trim_blocks=False,
    lstrip_blocks=False,
)


def render(name: str, **variables: object) -> str:
    """Render a bundled template by file name."""
    template_text = (
        resources
        # resources.files(None) anchors to this defining package, the same
        # package named here, so the None mutant is equivalent.
        .files("post_turn_quality_stop_hook.templates")  # pragma: no mutate
        .joinpath(name)
        .read_text()
    )
    template = _ENV.from_string(template_text)
    return template.render(**variables)
