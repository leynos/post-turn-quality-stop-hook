"""Remove credentials from remote URLs before display or logging."""

from __future__ import annotations

import re

#: User information in a URL-style remote, up to the last unescaped ``@`` in
#: the authority. Git splits userinfo at that ``@``, which lets a raw ``@``
#: survive in a password. Matching cannot cross whitespace or ``/``, so the
#: redaction stays inside the authority and never consumes diagnostic text.
_URL_USERINFO = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)[^\s/]*@")


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


def redact_remote(remote: str) -> str:
    """Remove user information from a remote URL before display or logging.

    Credentials can appear in ``branch.<name>.remote`` when a tokenised URL
    was used with ``git push -u``. The gate must never echo them in a block
    payload or warning. Remote names, local paths, and scp-style remotes such
    as ``git@github.com:owner/repo.git`` carry no URL userinfo and are
    returned unchanged.

    Parameters
    ----------
    remote
        Configured remote name, URL, or path.

    Returns
    -------
    str
        The remote with any ``scheme://userinfo@`` prefix removed.

    """
    return redact_credentials(remote)
