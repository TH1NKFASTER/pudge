"""The two model files load_model reads, from the Hugging Face cache or downloaded into it.

Pudge's own code (not vendored): it replaces huggingface_hub.snapshot_download, which
brought huggingface-hub and hf-xet into the install for two files. Models keep the cache
layout huggingface_hub uses (models--org--name/refs/main -> snapshots/<commit>/), so a
model either tool downloaded is found by the other without the network.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

import httpx

from ..branding import APP_SLUG
from ..i18n import err

ENDPOINT = "https://huggingface.co"
# load_model's own preference order.
WEIGHT_FILES = ("model.safetensors", "weights.safetensors", "weights.npz")
_REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*")
_COMMIT = re.compile(r"[0-9a-f]{40}")
_TRUE = {"1", "ON", "YES", "TRUE"}


class ModelDownloadError(RuntimeError):
    pass


def hub_cache() -> Path:
    """The cache folder huggingface_hub uses, from the same environment variables."""
    home = os.environ.get("HF_HOME") or os.path.join(
        os.environ.get("XDG_CACHE_HOME") or os.path.join("~", ".cache"), "huggingface"
    )
    cache = (
        os.environ.get("HF_HUB_CACHE") or os.environ.get("HUGGINGFACE_HUB_CACHE") or os.path.join(home, "hub")
    )
    return Path(os.path.expandvars(os.path.expanduser(cache)))


def _offline() -> bool:
    return os.environ.get("HF_HUB_OFFLINE", "").strip().upper() in _TRUE


def _repo_folder(cache: Path, repo_id: str) -> Path:
    return cache / ("models--" + repo_id.replace("/", "--"))


def _complete(snapshot: Path) -> bool:
    # huggingface_hub links snapshot files to blobs/; is_file() follows the links, so
    # a half-finished download (no link yet) does not count.
    return (snapshot / "config.json").is_file() and any((snapshot / name).is_file() for name in WEIGHT_FILES)


def cached_snapshot(repo_id: str, cache: Path | None = None) -> Path | None:
    folder = _repo_folder(cache or hub_cache(), repo_id)
    try:
        commit = (folder / "refs" / "main").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not _COMMIT.fullmatch(commit):
        return None
    snapshot = folder / "snapshots" / commit
    return snapshot if _complete(snapshot) else None


def _client() -> httpx.Client:
    # Proxies come from the environment (HTTPS_PROXY), as for Pudge's other clients; the
    # read timeout is per chunk, so a slow large download is not cut off.
    return httpx.Client(
        timeout=httpx.Timeout(60.0, connect=10.0),
        follow_redirects=True,
        headers={"User-Agent": APP_SLUG},
    )


def _download(client: httpx.Client, url: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".incomplete")
    try:
        with os.fdopen(handle, "wb") as output, client.stream("GET", url) as response:
            response.raise_for_status()
            for chunk in response.iter_bytes(1024 * 1024):
                output.write(chunk)
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _write_ref(folder: Path, commit: str) -> None:
    ref = folder / "refs" / "main"
    ref.parent.mkdir(parents=True, exist_ok=True)
    temporary = ref.with_name(ref.name + ".tmp")
    temporary.write_text(commit, encoding="utf-8")
    temporary.replace(ref)


def snapshot_download(repo_id: str, *, client: httpx.Client | None = None) -> str:
    """The local folder with config.json and the weights of `repo_id` ("org/name")."""
    cache = hub_cache()
    found = cached_snapshot(repo_id, cache)
    if found is not None:
        return str(found)
    if not _REPO.fullmatch(repo_id) or ".." in repo_id or "--" in repo_id:
        raise ModelDownloadError(err("stt.whisperModelName", model=repo_id))
    if _offline():
        raise ModelDownloadError(err("stt.whisperModelNotDownloaded", model=repo_id))
    owned = client is None
    http = client or _client()
    try:
        response = http.get(f"{ENDPOINT}/api/models/{repo_id}")
        response.raise_for_status()
        info = response.json()
        commit = str(info.get("sha") or "") if isinstance(info, dict) else ""
        siblings = info.get("siblings") if isinstance(info, dict) else None
        names = {str(item.get("rfilename")) for item in siblings or [] if isinstance(item, dict)}
        weights = next((name for name in WEIGHT_FILES if name in names), None)
        if not _COMMIT.fullmatch(commit) or "config.json" not in names or weights is None:
            raise ModelDownloadError(err("stt.whisperModelHasNoWeights", model=repo_id))
        folder = _repo_folder(cache, repo_id)
        snapshot = folder / "snapshots" / commit
        for name in ("config.json", weights):
            if not (snapshot / name).is_file():
                _download(http, f"{ENDPOINT}/{repo_id}/resolve/{commit}/{name}", snapshot / name)
        _write_ref(folder, commit)
    except (httpx.HTTPError, OSError, ValueError) as exc:
        raise ModelDownloadError(
            err("stt.whisperModelDownloadFailed", model=repo_id, reason=str(exc) or type(exc).__name__)
        ) from exc
    finally:
        if owned:
            http.close()
    return str(snapshot)
