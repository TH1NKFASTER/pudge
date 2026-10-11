"""Jiten and jpdb tokens live in the macOS Keychain like every other secret.

SQLite keeps an empty value. A token still found in the database (older
versions, or a system without the Keychain) is used as is and moved to the
Keychain the next time settings are read. Reads are cached per process, so the
`security` tool runs once per token, not on every settings read.
"""

from __future__ import annotations

import logging
import threading

from .secrets_store import SecretStore

KEYCHAIN_ACCOUNTS = {"jiten_api_key": "ln-jiten-api-key", "jpdb_api_token": "ln-jpdb-api-token"}

_LOGGER = logging.getLogger(__name__)
_STORE = SecretStore()
_CACHE: dict[str, str] = {}
_LOCK = threading.Lock()


def keychain_available() -> bool:
    return bool(_STORE.available)


def resolve(name: str, database_value: str) -> str:
    """The effective token: a database value (legacy) wins, else the Keychain."""
    value = str(database_value or "").strip()
    if value or name not in KEYCHAIN_ACCOUNTS or not keychain_available():
        return value
    with _LOCK:
        if name in _CACHE:
            return _CACHE[name]
    try:
        stored = str(_STORE.get(KEYCHAIN_ACCOUNTS[name]) or "").strip()
    except Exception as exc:  # noqa: BLE001 - an unreadable Keychain means "no token"
        _LOGGER.warning("FALLBACK step=study_secrets.read name=%s error=%r", name, exc)
        return ""
    with _LOCK:
        _CACHE[name] = stored
    return stored


def store(name: str, value: str) -> bool:
    """Save a token in the Keychain. True when the database copy may be blanked."""
    if name not in KEYCHAIN_ACCOUNTS or not keychain_available():
        return False
    value = str(value or "").strip()
    account = KEYCHAIN_ACCOUNTS[name]
    try:
        if value:
            ok = bool(_STORE.set(account, value))
        else:
            _STORE.delete(account)
            ok = True
    except Exception as exc:  # noqa: BLE001 - keep the database copy instead
        _LOGGER.warning("FALLBACK step=study_secrets.write name=%s error=%r", name, exc)
        return False
    if ok:
        with _LOCK:
            _CACHE[name] = value
    return ok


def forget_cache() -> None:
    with _LOCK:
        _CACHE.clear()
