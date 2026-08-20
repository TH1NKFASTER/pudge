from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass

from .branding import APP_BUNDLE_ID


SECRET_MASK = "••••••••"


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
        completed = subprocess.run(
            [
                "security",
                "add-generic-password",
                "-U",
                "-s",
                self.service,
                "-a",
                account,
                "-w",
                value,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
        return completed.returncode == 0

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
        return "" if use_keychain and secret and self.set(account, secret) else secret


def masked_secret(value: object) -> str:
    return SECRET_MASK if str(value or "") else ""


def keep_masked_secret(incoming: object, current: object) -> str:
    value = str(incoming or "")
    return str(current or "") if value == SECRET_MASK else value
