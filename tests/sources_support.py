"""Release sources for tests (Settings → Sources; Pudge ships with none).

Tests never name a real site: every address here is a reserved ``.invalid`` host.
A test that needs a working release search configures one::

    configure_sources(config)                 # sources.json next to config.config_path
    configure_sources(config, weekly_rss="")  # one part off
    release_search_client(proxy_mode="direct")
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pudge.providers.nyaa import NyaaClient
from pudge.sources import Sources, save_sources

RELEASE_SEARCH = "https://releases.invalid"
MIRRORS = ("https://mirror-a.invalid", "https://mirror-b.invalid")
WEEKLY_RSS = "https://weekly.invalid/rss"
RELEASE_FEED = "https://feed.invalid/feeds/site/"
RECOMMENDATIONS = "https://recommendations.invalid"


def sample_sources(**overrides: Any) -> Sources:
    values: dict[str, Any] = {
        "release_search": RELEASE_SEARCH,
        "mirrors": MIRRORS,
        "weekly_rss": WEEKLY_RSS,
        "release_feed": RELEASE_FEED,
        "recommendations": RECOMMENDATIONS,
    }
    values.update(overrides)
    values["mirrors"] = tuple(values["mirrors"])
    return Sources(**values)


def configure_sources(config: Any, **overrides: Any) -> Sources:
    """Write sources.json next to the config's config.toml (a temp folder in tests)."""
    from pudge.config import DEFAULT_CONFIG_PATH

    path = getattr(config, "config_path", None)
    # The default config path is shared by every test of the run.
    assert path is not None and Path(path).resolve() != DEFAULT_CONFIG_PATH.resolve(), (
        "set config.config_path to the test's temp folder first"
    )
    sources = sample_sources(**overrides)
    save_sources(config, sources)
    return sources


def release_search_client(**kwargs: Any) -> NyaaClient:
    return NyaaClient(RELEASE_SEARCH, mirrors=MIRRORS, **kwargs)
