#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


class ReleaseError(RuntimeError):
    pass


def run(
    *args: str,
    capture: bool = False,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    print("+", " ".join(args), flush=True)
    result = subprocess.run(
        args,
        cwd=ROOT,
        text=True,
        capture_output=capture,
    )
    if check and result.returncode:
        if capture:
            if result.stdout:
                print(result.stdout, end="")
            if result.stderr:
                print(result.stderr, end="", file=sys.stderr)
        raise ReleaseError(f"command failed ({result.returncode}): {' '.join(args)}")
    return result


def output(*args: str) -> str:
    return run(*args, capture=True).stdout.strip()


def git(*args: str, capture: bool = False, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run("git", *args, capture=capture, check=check)


def git_output(*args: str) -> str:
    return output("git", *args)


def current_version() -> str:
    payload = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return str(payload["project"]["version"])


def local_tag_sha(tag: str) -> str:
    result = git("rev-parse", "-q", "--verify", f"refs/tags/{tag}^{{}}", capture=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else ""


def remote_tag_sha(tag: str) -> str:
    result = git("ls-remote", "origin", f"refs/tags/{tag}^{{}}", capture=True)
    line = result.stdout.strip()
    if line:
        return line.split()[0]
    result = git("ls-remote", "origin", f"refs/tags/{tag}", capture=True)
    line = result.stdout.strip()
    return line.split()[0] if line else ""


def ensure_clean_index_after_stage() -> None:
    unstaged = git("diff", "--quiet", check=False).returncode
    untracked = git_output("ls-files", "--others", "--exclude-standard")
    if unstaged or untracked:
        raise ReleaseError(
            "working tree still has unstaged/untracked files after `git add -A`; "
            "release aborted"
        )


def ensure_origin_not_ahead() -> None:
    result = git("merge-base", "--is-ancestor", "origin/main", "HEAD", check=False)
    if result.returncode != 0:
        raise ReleaseError(
            "origin/main is not an ancestor of HEAD. Pull/rebase first; refusing "
            "to create a release from a stale/diverged checkout."
        )


def ensure_release_version(version: str, python: str) -> None:
    if current_version() != version:
        run(python, "scripts/bump_version.py", version)
    if current_version() != version:
        raise ReleaseError(f"version bump did not produce {version}")


def validate(python: str) -> None:
    # Cheap checks first, expensive test batches only if they pass.
    git("diff", "--check")
    run("make", "lint", f"PYTHON={python}")
    run("make", "test-batches", f"PYTHON={python}")
    git("diff", "--check")


def commit_if_needed(version: str) -> str:
    git("add", "-A")
    git("diff", "--cached", "--check")
    ensure_clean_index_after_stage()

    staged = git("diff", "--cached", "--quiet", check=False).returncode != 0
    if staged:
        run("git", "--no-pager", "diff", "--cached", "--stat")
        git("commit", "-m", f"Pudge v{version}")
    else:
        message = git_output("log", "-1", "--pretty=%s")
        if message != f"Pudge v{version}":
            raise ReleaseError(
                "nothing is staged and HEAD is not the requested release commit"
            )

    head = git_output("rev-parse", "HEAD")
    if current_version() != version:
        raise ReleaseError("release commit does not contain the requested version")
    return head


def publish(version: str, head: str) -> None:
    tag = f"v{version}"

    git("push", "origin", "main")

    existing_local = local_tag_sha(tag)
    if existing_local and existing_local != head:
        raise ReleaseError(
            f"local {tag} already points to {existing_local[:12]}, not HEAD {head[:12]}"
        )
    if not existing_local:
        git("tag", tag)

    existing_remote = remote_tag_sha(tag)
    if existing_remote and existing_remote != head:
        raise ReleaseError(
            f"remote {tag} already points to {existing_remote[:12]}, not HEAD {head[:12]}"
        )
    if not existing_remote:
        git("push", "origin", tag)

    print()
    print(f"Released Pudge v{version}")
    print(f"commit: {head}")
    print(f"tag:    {tag}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate, commit, push and tag a Pudge release."
    )
    parser.add_argument("version", help="Release version, e.g. 0.7.21")
    parser.add_argument(
        "--python",
        default=os.environ.get("PUDGE_RELEASE_PYTHON") or sys.executable,
        help="Python interpreter used by lint/tests/bump",
    )
    parser.add_argument(
        "--no-push",
        action="store_true",
        help="Validate and create the release commit, but do not push/tag",
    )
    args = parser.parse_args()

    version = str(args.version).strip()
    if not SEMVER.fullmatch(version):
        raise ReleaseError("VERSION must look like X.Y.Z")

    if git_output("rev-parse", "--show-toplevel") != str(ROOT):
        raise ReleaseError("run from the Pudge checkout")

    branch = git_output("branch", "--show-current")
    if branch != "main":
        raise ReleaseError(f"release must run from main, current branch is {branch!r}")

    tag = f"v{version}"
    print(f"Pudge release {tag}")
    print(f"checkout: {ROOT}")

    git("fetch", "origin", "main", "--tags")
    ensure_origin_not_ahead()

    remote = remote_tag_sha(tag)
    if remote:
        head = git_output("rev-parse", "HEAD")
        if remote == head and current_version() == version:
            print(f"{tag} is already published at HEAD; nothing to do.")
            return 0
        raise ReleaseError(f"remote tag {tag} already exists")

    ensure_release_version(version, args.python)
    validate(args.python)
    head = commit_if_needed(version)

    if args.no_push:
        print(f"Release commit ready at {head}; push/tag skipped.")
        return 0

    publish(version, head)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ReleaseError as exc:
        print(f"release aborted: {exc}", file=sys.stderr)
        raise SystemExit(2)
