"""Remove credentials from remote URLs before display or logging.

`upstream.py` uses these helpers for fetch diagnostics and `pipeline.py` uses
them for the unpushed block label.
"""

from __future__ import annotations

import re

#: User information in a URL-style remote, up to the last unescaped ``@`` in
#: the authority. Git splits userinfo at that ``@``, which lets a raw ``@``
#: survive in a password. Matching cannot cross whitespace, ``/``, ``?``, or
#: ``#``: the last three start the path, query, and fragment, so an ``@`` there
#: belongs to the repository or its parameters, never to userinfo. Keeping the
#: match inside the authority stops a query such as ``?email=ops@example.org``
#: from eating the real hostname.
_URL_USERINFO = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)[^\s/?#]*@")

#: A remote with an explicit URL scheme. Only those can carry userinfo; a
#: configured remote name, a local path, and an scp-style ``host:path`` remote
#: cannot.
_URL_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")

#: A Git remote-helper address, ``<transport>::<address>``. Git accepts an
#: arbitrary address, so this can itself be a URL and carry userinfo even
#: though the whole value does not begin with a scheme.
_REMOTE_HELPER = re.compile(
    r"^(?P<transport>[A-Za-z0-9][A-Za-z0-9+.-]*)::(?P<address>[\s\S]*)$"
)


def redact_credentials(text: str) -> str:
    """Remove URL user information from arbitrary text.

    Git diagnostics can repeat a configured URL in a shortened form that omits
    the repository path, so replacing the configured remote verbatim is not
    enough. Applying the pattern to the whole message removes every
    ``scheme://userinfo@`` occurrence while leaving unrelated text intact.

    Parameters
    ----------
    text
        Text that may contain URL-style remotes.

    Returns
    -------
    str
        The text with all URL user information removed.

    """
    return _URL_USERINFO.sub(r"\g<scheme>", text)


def _display_part(text: str) -> str | None:
    """Redact URL userinfo from ``text``, withholding it on a residual ``@``."""
    redacted = redact_credentials(text)
    return None if "@" in redacted else redacted


def redact_remote_safely(remote: str) -> str | None:
    """Return a display form of a remote, or ``None`` when none is safe.

    A well-formed URL loses its userinfo and keeps its host and path. A URL
    that still contains ``@`` after redaction cannot be rendered safely: an
    unescaped ``/`` in a password, as in ``https://user:pa/ss@host/repo``,
    leaves part of the credential in what looks like the path, so no part of
    that remote is shown. The same rule covers a Git remote-helper address,
    ``<transport>::<address>``: its address is free-form and may itself be a
    URL, so it is redacted as a whole and withheld on any residual ``@``
    rather than risk a credential in a part this module cannot model.
    Configured remote names, local paths, and scp-style remotes carry no URL
    userinfo and are returned unchanged.

    Parameters
    ----------
    remote
        Configured remote name, URL, path, or remote-helper address.

    Returns
    -------
    str | None
        A display-safe form, or ``None`` when no part of the remote can be
        shown without risking a credential.

    """
    helper = _REMOTE_HELPER.match(remote)
    if helper is not None:
        address = _display_part(helper.group("address"))
        if address is None:
            return None
        return f"{helper.group('transport')}::{address}"
    if not _URL_SCHEME.match(remote):
        return remote
    return _display_part(remote)


def redact_text_safely(text: str) -> str | None:
    """Return redacted diagnostics, or ``None`` when they may hold a secret.

    A named remote hides its URL in configuration, so it says nothing about
    whether Git can repeat a credential-bearing URL in its own diagnostics.
    The safe form is therefore decided from the text: redaction removes
    well-formed userinfo, and a residual ``@`` marks the same malformed-URL
    case that :func:`redact_remote_safely` withholds, so the whole message is
    withheld rather than risk echoing part of a password.

    Parameters
    ----------
    text
        Diagnostic output that may repeat a configured remote URL.

    Returns
    -------
    str | None
        The redacted text, or ``None`` when it cannot be shown safely.

    """
    return _display_part(text)
