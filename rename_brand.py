#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BRAND_FILE = ROOT / "anime_mpv" / "brand.env"


def default_slug(name: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")
    return value or "app"


def read_brand() -> dict[str, str]:
    values: dict[str, str] = {}
    if not BRAND_FILE.is_file():
        return values
    for line in BRAND_FILE.read_text(encoding="utf-8").splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("\"'")
    return values


def legacy_values(values: dict[str, str], key: str, previous: str, current: str) -> str:
    result = [item.strip() for item in values.get(key, "").split("|") if item.strip()]
    if previous and previous != current and previous not in result:
        result.append(previous)
    return "|".join(result)


def main() -> int:
    parser = argparse.ArgumentParser(description="Change product branding from one central file.")
    parser.add_argument("name", help="Visible macOS/app name")
    parser.add_argument("--slug", help="Folder/qBittorrent/release slug")
    parser.add_argument(
        "--bundle-id",
        help="macOS bundle identifier. By default the existing identifier is preserved so permissions survive a rename.",
    )
    parser.add_argument("--cli", help="CLI command name")
    parser.add_argument("--agent-cli", help="Agent CLI command name")
    args = parser.parse_args()

    old = read_brand()
    slug = (args.slug or default_slug(args.name)).strip()
    bundle = (args.bundle_id or old.get("APP_BUNDLE_ID") or f"com.{slug}.app").strip()
    cli = (args.cli or slug).strip()
    agent_cli = (args.agent_cli or f"{cli}-agent").strip()
    env_prefix = re.sub(r"[^A-Z0-9]+", "_", slug.upper()).strip("_") or "APP"

    legacy_names = legacy_values(old, "APP_LEGACY_NAMES", old.get("APP_NAME", ""), args.name)
    legacy_slugs = legacy_values(old, "APP_LEGACY_SLUGS", old.get("APP_SLUG", ""), slug)
    legacy_bundles = legacy_values(old, "APP_LEGACY_BUNDLE_IDS", old.get("APP_BUNDLE_ID", ""), bundle)
    legacy_clis = legacy_values(old, "APP_LEGACY_CLIS", old.get("APP_CLI", ""), cli)
    legacy_agents = legacy_values(old, "APP_LEGACY_AGENT_CLIS", old.get("APP_AGENT_CLI", ""), agent_cli)

    BRAND_FILE.write_text(
        "\n".join(
            [
                f'APP_NAME="{args.name}"',
                f'APP_SLUG="{slug}"',
                f'APP_BUNDLE_ID="{bundle}"',
                f'APP_CLI="{cli}"',
                f'APP_AGENT_CLI="{agent_cli}"',
                f'APP_ENV_PREFIX="{env_prefix}"',
                f'APP_LEGACY_NAMES="{legacy_names}"',
                f'APP_LEGACY_SLUGS="{legacy_slugs}"',
                f'APP_LEGACY_BUNDLE_IDS="{legacy_bundles}"',
                f'APP_LEGACY_CLIS="{legacy_clis}"',
                f'APP_LEGACY_AGENT_CLIS="{legacy_agents}"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    example = ROOT / "config.example.toml"
    if example.is_file():
        text = example.read_text(encoding="utf-8")
        old_name = old.get("APP_NAME", "Anime MPV")
        old_slug = old.get("APP_SLUG", "anime-mpv")
        text = text.replace(f"~/Library/Caches/{old_slug}", f"~/Library/Caches/{slug}")
        text = text.replace(f"~/.local/share/{old_slug}", f"~/.local/share/{slug}")
        text = text.replace(f"~/Movies/{old_name}", f"~/Movies/{args.name}")
        text = text.replace(f'category = "{old_slug}"', f'category = "{slug}"')
        example.write_text(text, encoding="utf-8")

    print(BRAND_FILE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
