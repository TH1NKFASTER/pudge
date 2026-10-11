from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass

from .branding import APP_BUNDLE_ID
from .i18n import err


SECRET_MASK = "••••••••"
# Service and account names are written unquoted into a `security -i` command line.
_KEYCHAIN_NAME = re.compile(r"[A-Za-z0-9._@-]+")


@dataclass(slots=True)
class SecretStore:
    """Small macOS Keychain adapter with an explicit portable fallback."""

    service: str = APP_BUNDLE_ID

    @property
    def available(self) -> bool:
        return sys.platform == "darwin" and shutil.which("security") is not None

    def get(self, account: str) -> str:
        if not self.available:
            return ""
        completed = subprocess.run(
            ["security", "find-generic-password", "-s", self.service, "-a", account, "-w"],
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
        return completed.stdout.rstrip("\n") if completed.returncode == 0 else ""

    def set(self, account: str, value: str) -> bool:
        if not self.available or not value:
            return False
        if not (_KEYCHAIN_NAME.fullmatch(self.service) and _KEYCHAIN_NAME.fullmatch(account)):
            return False
        if self.get(account) == value:
            return True
        # The secret goes to `security` on stdin (interactive mode, as hex data):
        # a command-line argument is readable by every process through `ps`.
        command = f"add-generic-password -U -s {self.service} -a {account} -X {value.encode('utf-8').hex()}\n"
        completed = subprocess.run(
            ["security", "-i"],
            input=command,
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
        # Interactive mode may not report a failed command in its exit status, and a
        # value `find-generic-password -w` would print differently (as hex) must stay
        # in the config file rather than come back wrong: read it back.
        if completed.returncode == 0 and self.get(account) == value:
            return True
        logging.getLogger("pudge.secrets").warning(
            "FALLBACK step=keychain.write account=%s returncode=%s", account, completed.returncode
        )
        return False

    def delete(self, account: str) -> None:
        if self.available:
            completed = subprocess.run(
                ["security", "delete-generic-password", "-s", self.service, "-a", account],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
            # security uses 44 for an absent item; clearing it is already done.
            if completed.returncode not in {0, 44}:
                raise RuntimeError(err("setup.keychainDeleteFailed", account=account))

    def resolve(
        self,
        account: str,
        config_value: object,
        *,
        env: str = "",
        use_keychain: bool = True,
    ) -> str:
        environment = os.getenv(env, "").strip() if env else ""
        if environment:
            return environment
        stored = self.get(account) if use_keychain else ""
        if stored:
            return stored
        legacy = str(config_value or "").strip()
        if legacy and use_keychain:
            self.set(account, legacy)
        return legacy

    def persisted_config_value(self, account: str, value: object, *, use_keychain: bool = True) -> str:
        secret = str(value or "")
        if use_keychain and not secret:
            self.delete(account)
            return ""
        return "" if use_keychain and secret and self.set(account, secret) else secret


def masked_secret(value: object) -> str:
    return SECRET_MASK if str(value or "") else ""


def keep_masked_secret(incoming: object, current: object) -> str:
    value = str(incoming or "")
    return str(current or "") if value == SECRET_MASK else value
