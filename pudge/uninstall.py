from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .branding import (
    APP_AGENT_CLI,
    APP_BUNDLE_ID,
    APP_CLI,
    APP_HOME,
    APP_NAME,
    APP_SLUG,
    LEGACY_AGENT_CLIS,
    LEGACY_APP_CLIS,
    LEGACY_APP_NAMES,
    LEGACY_APP_SLUGS,
    LEGACY_BUNDLE_IDS,
)


@dataclass(frozen=True, slots=True)
class UninstallPlan:
    targets: tuple[Path, ...]
    launch_agent_labels: tuple[str, ...]
    app_bundles: tuple[Path, ...]
    process_names: tuple[str, ...]
    keychain_services: tuple[str, ...]


def _unique_paths(paths: list[Path]) -> tuple[Path, ...]:
    seen: set[str] = set()
    result: list[Path] = []
    for path in paths:
        normalized = Path(os.path.abspath(str(path.expanduser())))
        key = str(normalized)
        if key not in seen:
            seen.add(key)
            result.append(normalized)
    return tuple(result)


def _looks_app_owned_directory(path: Path, *, home: Path) -> bool:
    normalized = Path(os.path.abspath(str(path.expanduser())))
    forbidden = {
        Path("/"),
        home,
        home / "Applications",
        home / "Desktop",
        home / "Documents",
        home / "Downloads",
        home / "Library",
        home / "Movies",
        home / ".config",
        home / ".local",
        home / ".local" / "share",
    }
    names = {APP_NAME.casefold(), APP_SLUG.casefold()}
    names.update(value.casefold() for value in LEGACY_APP_NAMES)
    names.update(value.casefold() for value in LEGACY_APP_SLUGS)
    return normalized not in forbidden and normalized.name.casefold() in names


def build_uninstall_plan(
    *,
    home: Path = APP_HOME,
    config_path: Path | None = None,
    cache_dir: Path | None = None,
    database_path: Path | None = None,
    library_root: Path | None = None,
) -> UninstallPlan:
    """Return the exact Pudge-owned paths removed by the in-app uninstaller.

    Arbitrary watched folders and shared third-party tools are deliberately not
    included. A configured cache or media directory is removed only when its
    final directory name is a current or legacy Pudge brand name.
    """

    home = Path(os.path.abspath(str(home.expanduser())))
    app_names = (APP_NAME, *LEGACY_APP_NAMES)
    slugs = (APP_SLUG, *LEGACY_APP_SLUGS)
    bundle_ids = (APP_BUNDLE_ID, *LEGACY_BUNDLE_IDS)
    cli_names = (APP_CLI, APP_AGENT_CLI, *LEGACY_APP_CLIS, *LEGACY_AGENT_CLIS)
    launch_agent_labels = tuple(
        dict.fromkeys(f"{bundle_id.removesuffix('.app')}.agent" for bundle_id in bundle_ids)
    )

    app_bundles = _unique_paths(
        [home / "Applications" / f"{name}.app" for name in app_names]
        + [home / "Applications" / f"{name} Settings.app" for name in app_names]
    )
    targets: list[Path] = list(app_bundles)
    targets.extend(home / ".local" / "bin" / name for name in cli_names)
    targets.extend(home / "Library" / "LaunchAgents" / f"{label}.plist" for label in launch_agent_labels)

    for slug in slugs:
        targets.extend(
            (
                home / ".config" / slug,
                home / ".local" / "share" / slug,
                home / "Library" / "Caches" / slug,
                home / "Library" / "Logs" / slug,
                home / "Library" / "Logs" / f"{slug}-runtime.log",
                home / "Library" / "Logs" / f"{slug}-energy.jsonl",
                home / "Library" / "Logs" / f"{slug}-agent.log",
                home / "Library" / "Logs" / f"{slug}-agent-error.log",
                home / "Library" / "Logs" / f"{slug}-update.log",
                home / "Library" / "Logs" / f"{slug}-manga-ocr-install.log",
            )
        )
        targets.extend(sorted((home / "Downloads").glob(f"{slug}-backup-*.zip")))
    for name in app_names:
        targets.extend(
            (
                home / "Movies" / name,
                home / "Library" / "Application Support" / name,
            )
        )
    for bundle_id in bundle_ids:
        targets.extend(
            (
                home / "Library" / "Application Support" / bundle_id,
                home / "Library" / "Application Scripts" / bundle_id,
                home / "Library" / "Caches" / bundle_id,
                home / "Library" / "Containers" / bundle_id,
                home / "Library" / "Cookies" / f"{bundle_id}.binarycookies",
                home / "Library" / "HTTPStorages" / bundle_id,
                home / "Library" / "HTTPStorages" / f"{bundle_id}.binarycookies",
                home / "Library" / "Preferences" / f"{bundle_id}.plist",
                home / "Library" / "Saved Application State" / f"{bundle_id}.savedState",
                home / "Library" / "WebKit" / bundle_id,
                home
                / "Library"
                / "Application Support"
                / "com.apple.sharedfilelist"
                / "com.apple.LSSharedFileList.ApplicationRecentDocuments"
                / f"{bundle_id}.sfl2",
                home
                / "Library"
                / "Application Support"
                / "com.apple.sharedfilelist"
                / "com.apple.LSSharedFileList.ApplicationRecentDocuments"
                / f"{bundle_id}.sfl3",
            )
        )

    if config_path is not None:
        targets.append(config_path)
    if database_path is not None:
        database = Path(database_path).expanduser()
        targets.extend((database, Path(f"{database}-shm"), Path(f"{database}-wal")))
    for candidate in (cache_dir, library_root):
        if candidate is not None and _looks_app_owned_directory(candidate, home=home):
            targets.append(candidate)

    exact_targets = _unique_paths(targets)
    unsafe = [path for path in exact_targets if path == Path("/") or path == home]
    if unsafe:
        raise ValueError(f"Refusing unsafe uninstall target: {unsafe[0]}")
    return UninstallPlan(
        targets=exact_targets,
        launch_agent_labels=launch_agent_labels,
        app_bundles=app_bundles,
        process_names=tuple(dict.fromkeys((*app_names, *cli_names))),
        keychain_services=tuple(dict.fromkeys(bundle_ids)),
    )


def render_uninstall_script(plan: UninstallPlan, *, parent_pid: int) -> str:
    quote = shlex.quote
    lsregister_path = (
        "/System/Library/Frameworks/CoreServices.framework/Frameworks/"
        "LaunchServices.framework/Support/lsregister"
    )
    lines = [
        "#!/bin/zsh",
        "set -u",
        f"parent_pid={int(parent_pid)}",
        "for attempt in {1..240}; do",
        "  /bin/kill -0 $parent_pid >/dev/null 2>&1 || break",
        "  /bin/sleep 0.25",
        "done",
    ]
    for label in plan.launch_agent_labels:
        plist = next(
            (path for path in plan.targets if path.name == f"{label}.plist"),
            None,
        )
        if plist is not None:
            lines.append(
                f"/bin/launchctl bootout gui/$(/usr/bin/id -u) {quote(str(plist))} >/dev/null 2>&1 || true"
            )
        lines.append(f"/bin/launchctl remove {quote(label)} >/dev/null 2>&1 || true")
    for name in plan.process_names:
        lines.append(f"/usr/bin/pkill -x {quote(name)} >/dev/null 2>&1 || true")
    lines.extend(
        (
            f"lsregister={quote(lsregister_path)}",
            'if [[ -x "$lsregister" ]]; then',
        )
    )
    for bundle in plan.app_bundles:
        lines.append(f'  "$lsregister" -u {quote(str(bundle))} >/dev/null 2>&1 || true')
    lines.append("fi")
    for service in plan.keychain_services:
        lines.extend(
            (
                f"while /usr/bin/security delete-generic-password -s {quote(service)} >/dev/null 2>&1; do",
                "  :",
                "done",
            )
        )
        lines.append(
            f"/usr/bin/tccutil reset All {quote(service)} >/dev/null 2>&1 || true"
        )
    for target in plan.targets:
        lines.append(f"/bin/rm -rf -- {quote(str(target))}")
    lines.append('/bin/rm -f -- "$0"')
    return "\n".join(lines) + "\n"


def launch_uninstaller(plan: UninstallPlan, *, parent_pid: int | None = None) -> Path:
    descriptor, raw_path = tempfile.mkstemp(prefix=f"{APP_SLUG}-uninstall-", suffix=".zsh")
    script_path = Path(raw_path)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(render_uninstall_script(plan, parent_pid=parent_pid or os.getpid()))
        script_path.chmod(0o700)
        subprocess.Popen(
            ["/bin/zsh", str(script_path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        script_path.unlink(missing_ok=True)
        raise
    return script_path
