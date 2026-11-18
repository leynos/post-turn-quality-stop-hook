"""Exercise credential redaction for remote URLs, helper addresses, diagnostics."""

from __future__ import annotations

import pytest

from post_turn_quality_stop_hook import redaction as redaction_mod


class TestRedactCredentials:
    """Tests for redact_credentials()."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            (
                "https://user:secret@example.invalid/repo.git",
                "https://example.invalid/repo.git",
            ),
            (
                "https://token@example.invalid/repo.git",
                "https://example.invalid/repo.git",
            ),
            ("ssh://git@example.invalid/repo.git", "ssh://example.invalid/repo.git"),
            (
                # Git splits userinfo at the last ``@``, so a raw ``@`` can
                # survive inside a password.
                "https://user:p@ss@example.invalid/repo.git",
                "https://example.invalid/repo.git",
            ),
            (
                # An ``@`` in a query belongs to the repository, not to
                # userinfo, so the hostname must survive.
                "https://example.invalid/repo.git?email=ops@example.org",
                "https://example.invalid/repo.git?email=ops@example.org",
            ),
            (
                "https://example.invalid/repo.git#ops@example.org",
                "https://example.invalid/repo.git#ops@example.org",
            ),
            (
                # Userinfo is still removed when a query follows it.
                "https://token@example.invalid/repo.git?ref=main",
                "https://example.invalid/repo.git?ref=main",
            ),
            (
                # Git can repeat the URL inside a longer diagnostic.
                "fatal: unable to access 'https://token@example.invalid/r.git/'",
                "fatal: unable to access 'https://example.invalid/r.git/'",
            ),
        ],
    )
    def test_removes_url_userinfo(self, text: str, expected: str) -> None:
        """Only URL-style userinfo is stripped."""
        assert redaction_mod.redact_credentials(text) == expected, (
            f"redact_credentials({text!r}) must be {expected!r}"
        )


class TestRedactRemoteSafely:
    """Tests for redact_remote_safely()."""

    @pytest.mark.parametrize(
        ("remote", "expected"),
        [
            # A remote name, a local path, and an scp-style remote cannot
            # carry URL userinfo, so they are shown unchanged.
            ("origin", "origin"),
            ("/srv/git/hub.git", "/srv/git/hub.git"),
            ("git@github.com:owner/repo.git", "git@github.com:owner/repo.git"),
            (
                "https://token@example.invalid/repo.git",
                "https://example.invalid/repo.git",
            ),
            (
                # A query or fragment with no ``@`` survives redaction.
                "https://token@example.invalid/repo.git?ref=main",
                "https://example.invalid/repo.git?ref=main",
            ),
            (
                # A remote-helper address can itself be a URL; its userinfo
                # must be removed while the transport prefix is kept.
                "https::https://token@example.invalid/repo.git",
                "https::https://example.invalid/repo.git",
            ),
            # A helper address that carries no URL is shown unchanged.
            ("ext::sh -c transport", "ext::sh -c transport"),
            ("fd::17", "fd::17"),
        ],
    )
    def test_returns_a_display_safe_remote(self, remote: str, expected: str) -> None:
        """A remote with no residual credential is rendered for display."""
        assert redaction_mod.redact_remote_safely(remote) == expected, (
            f"redact_remote_safely({remote!r}) must be {expected!r}"
        )

    @pytest.mark.parametrize(
        "remote",
        [
            # An unescaped ``/`` in the password stops the userinfo match
            # before the ``@``, so part of the secret lands in what looks
            # like the path. Nothing from this remote may be shown.
            "https://user:pa/ss@example.invalid/repo.git",
            "https://user:pa/ss@example.invalid:8443/repo.git",
            # The guard is deliberately conservative: an ``@`` after a valid
            # URL's query is not a credential, but it is indistinguishable
            # from one that a malformed URL left behind, so the whole remote
            # is withheld rather than risk echoing a secret.
            "https://example.invalid/repo.git?email=ops@example.org",
            # A helper address is free-form, so the same conservative guard
            # applies to it: this scp-style address is probably harmless, but
            # nothing in the address can prove a credential is absent.
            "https::https://user:pa/ss@example.invalid/repo.git",
            "ext::ssh git@github.com",
        ],
    )
    def test_withholds_a_remote_with_a_residual_at_sign(self, remote: str) -> None:
        """A remote that cannot be redacted completely is not rendered at all."""
        assert redaction_mod.redact_remote_safely(remote) is None, (
            f"redact_remote_safely({remote!r}) must withhold the remote"
        )


class TestRedactTextSafely:
    """Tests for redact_text_safely()."""

    def test_redacts_a_well_formed_url_in_diagnostics(self) -> None:
        """A diagnostic that repeats a valid URL keeps its host and path."""
        text = "fatal: unable to access 'https://token@example.invalid/r.git/'"
        assert redaction_mod.redact_text_safely(text) == (
            "fatal: unable to access 'https://example.invalid/r.git/'"
        ), "a well-formed URL must lose its userinfo and keep its host"

    def test_redacts_a_helper_address_url_in_diagnostics(self) -> None:
        """A URL inside a remote-helper address is redacted like any other."""
        text = "fatal: unable to access 'https::https://token@example.invalid/r.git'"
        assert redaction_mod.redact_text_safely(text) == (
            "fatal: unable to access 'https::https://example.invalid/r.git'"
        ), "a URL inside a helper address must lose its userinfo"

    def test_keeps_text_without_a_url(self) -> None:
        """Ordinary diagnostics pass through unchanged."""
        assert redaction_mod.redact_text_safely("fatal: repository not found") == (
            "fatal: repository not found"
        ), "a diagnostic without userinfo must be preserved"

    def test_withholds_text_with_a_residual_at_sign(self) -> None:
        """A malformed URL leaves a residual ``@``, so nothing is returned."""
        text = "fatal: unable to access 'https://user:pa/ss@example.invalid/r.git/'"
        assert redaction_mod.redact_text_safely(text) is None, (
            "diagnostics with a residual credential marker must be withheld"
        )
