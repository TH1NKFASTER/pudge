"""Credentials inside URLs (``scheme://user:password@host``): log, show and keep them safely.

A proxy address such as ``socks5://alice:secret@vpn.example:1080`` used to reach
the runtime log, Copy diagnostics and debug bundles as it was: redaction only
knew ``http(s)://``. Logs now show such an address as ``scheme://host:port``,
any ``scheme://userinfo@`` left in a text is scrubbed, and the Settings window
gets the address with the password masked (a masked password sent back means
"keep the saved one").
"""

from __future__ import annotations

import re

from .secrets_store import SECRET_MASK

# Any URL scheme (socks5h://, https://, redis://...), then userinfo up to "@".
# The userinfo stops at characters a URL authority cannot contain unencoded.
_URL_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s/?#@\"'<>\\]+@")


def redact_url_credentials(text: str) -> str:
    """``text`` with the userinfo of every ``scheme://user:pass@`` URL replaced."""
    value = str(text or "")
    if "@" not in value or "://" not in value:
        return value
    return _URL_USERINFO.sub(r"\1[REDACTED]@", value)


def _split(url: str) -> tuple[str, str, str, str]:
    """(scheme with "://" or "", userinfo, host[:port], rest) without any decoding."""
    value = str(url or "").strip()
    scheme, separator, remainder = value.partition("://")
    if not separator:
        scheme, remainder = "", value
    else:
        scheme += separator
    cut = len(remainder)
    for marker in "/?#":
        index = remainder.find(marker)
        if index != -1:
            cut = min(cut, index)
    authority, rest = remainder[:cut], remainder[cut:]
    userinfo, at, hostport = authority.rpartition("@")
    if not at:
        userinfo, hostport = "", authority
    return scheme, userinfo, hostport, rest


def url_for_log(url: str) -> str:
    """``scheme://host:port`` of an address (a proxy) for logs and error texts."""
    scheme, _userinfo, hostport, _rest = _split(url)
    return f"{scheme}{hostport}" if hostport else ""


def mask_url_password(url: str) -> str:
    """The address with its password shown as the Settings mask (the user name stays)."""
    scheme, userinfo, hostport, rest = _split(url)
    user, colon, password = userinfo.partition(":")
    if not colon or not password:
        return str(url or "").strip()
    return f"{scheme}{user}:{SECRET_MASK}@{hostport}{rest}"


def _address_and_user(url: str) -> tuple[str, str, str]:
    """(scheme, host[:port], user) compared case-insensitively where URLs are."""
    scheme, userinfo, hostport, _rest = _split(url)
    return scheme.casefold(), hostport.casefold(), userinfo.partition(":")[0]


def restore_url_password(incoming: str, current: str) -> str:
    """A masked password in ``incoming`` (sent back from the form) becomes the saved one.

    Only for the same scheme, host, port and user name: with the host edited, the
    saved password would go to a server the user never gave it to, so the mask
    then counts as no password.
    """
    value = str(incoming or "").strip()
    scheme, userinfo, hostport, rest = _split(value)
    user, colon, password = userinfo.partition(":")
    if not colon or password != SECRET_MASK:
        return value
    _scheme, current_userinfo, _hostport, _rest = _split(current)
    _user, current_colon, current_password = current_userinfo.partition(":")
    if current_colon and current_password and _address_and_user(value) == _address_and_user(current):
        return f"{scheme}{user}:{current_password}@{hostport}{rest}"
    return f"{scheme}{user}@{hostport}{rest}" if user else f"{scheme}{hostport}{rest}"


def same_address_apart_from_password(first: str, second: str) -> bool:
    """Whether two addresses differ at most in the password (the path counts too)."""
    if _address_and_user(first) != _address_and_user(second):
        return False
    return _split(first)[3].rstrip("/") == _split(second)[3].rstrip("/")


def same_server(first: str, second: str) -> bool:
    """Whether two addresses name the same scheme and host[:port] (an empty one never matches).

    A saved API key kept behind the Settings mask goes only to the server it was saved
    for: a test call with another address must not send it there.
    """
    scheme, _userinfo, hostport, _rest = _split(first)
    other_scheme, _other_userinfo, other_hostport, _other_rest = _split(second)
    if not hostport or not other_hostport:
        return False
    return (scheme.casefold(), hostport.casefold()) == (other_scheme.casefold(), other_hostport.casefold())
