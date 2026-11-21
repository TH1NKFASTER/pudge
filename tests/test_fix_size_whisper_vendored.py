"""Japanese STT without torch, numba, scipy and huggingface-hub (size reduction).

Before: the ``sync`` extra installed mlx-whisper 0.4.3, which pulled in torch with sympy,
networkx and jinja2 (about 630 MB, used only to convert models), numba with llvmlite
(about 155 MB, for one DTW loop), scipy (about 100 MB, for one median filter), tqdm (whose
bar the STT worker patched to read progress) and huggingface-hub with hf-xet (to fetch two
model files).

Now: the parts Pudge runs are vendored as pudge.whisper_mlx (MIT, Apple) and need only mlx,
tiktoken and numpy:

* word-timestamp DTW and the attention median filter are numpy code with exactly the
  upstream results (same tie-breaking, same cropped medfilt output);
* transcribe() reports progress through a callback with tqdm's running count, and the worker
  writes the same progress file as before;
* models come from the Hugging Face cache layout (a model downloaded by mlx-whisper is found
  without the network) or are downloaded into it with httpx; HF_HUB_OFFLINE is respected and a
  failed download reaches the user through the worker's existing "MLX Whisper failed" path;
* importing pudge.whisper_mlx needs nothing; ``stt_worker --check`` is available only with
  mlx and tiktoken.
"""

from __future__ import annotations

import ast
import fnmatch
import importlib
import json
import sys
import time
import tomllib
import types
from pathlib import Path
from types import SimpleNamespace

import httpx
import numpy as np
import pytest

import pudge
import source_policy
from pudge import i18n
from pudge.branding import APP_SLUG
from pudge.subtitles import stt_worker
from pudge.whisper_mlx import hub

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "pudge.whisper_mlx"
REPO = "mlx-community/whisper-tiny"
COMMIT = "0123456789abcdef0123456789abcdef01234567"


def _forget_package(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in [name for name in sys.modules if name == PACKAGE or name.startswith(PACKAGE + ".")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.delattr(pudge, "whisper_mlx", raising=False)


def _drop_fresh_imports() -> None:
    for name in [name for name in sys.modules if name == PACKAGE or name.startswith(PACKAGE + ".")]:
        del sys.modules[name]
    if "whisper_mlx" in vars(pudge):
        delattr(pudge, "whisper_mlx")


def _placeholder(name: str) -> type:
    return type(name, (), {})


@pytest.fixture
def vendored(monkeypatch: pytest.MonkeyPatch):
    """pudge.whisper_mlx imported afresh with stand-ins for mlx and tiktoken (not on Linux CI)."""
    _forget_package(monkeypatch)
    core = types.ModuleType("mlx.core")
    core.__getattr__ = _placeholder  # mx.array, mx.float16, ... in annotations and defaults
    core.compile = lambda function: function
    nn = types.ModuleType("mlx.nn")
    nn.__getattr__ = _placeholder
    utils = types.ModuleType("mlx.utils")
    utils.__getattr__ = _placeholder
    mlx = types.ModuleType("mlx")
    mlx.core, mlx.nn, mlx.utils = core, nn, utils
    tiktoken = types.ModuleType("tiktoken")
    tiktoken.__getattr__ = _placeholder
    for name, module in (
        ("mlx", mlx),
        ("mlx.core", core),
        ("mlx.nn", nn),
        ("mlx.utils", utils),
        ("tiktoken", tiktoken),
    ):
        monkeypatch.setitem(sys.modules, name, module)
    yield lambda name: importlib.import_module(f"{PACKAGE}.{name}")
    _drop_fresh_imports()


@pytest.fixture
def without_mlx(monkeypatch: pytest.MonkeyPatch):
    """A Mac without the sync extra: mlx and tiktoken cannot be imported."""
    _forget_package(monkeypatch)
    for name in ("mlx", "mlx.core", "mlx.nn", "mlx.utils", "tiktoken"):
        monkeypatch.setitem(sys.modules, name, None)
    yield
    _drop_fresh_imports()


# ---------------------------------------------------------------- upstream references
# mlx-whisper 0.4.3 timing.py as written there, without @numba.jit and with
# scipy.signal.medfilt spelled out (zero-padded borders, middle of each sorted window).


def _upstream_backtrace(trace: np.ndarray) -> np.ndarray:
    i = trace.shape[0] - 1
    j = trace.shape[1] - 1
    trace[0, :] = 2
    trace[:, 0] = 1
    result = []
    while i > 0 or j > 0:
        result.append((i - 1, j - 1))
        if trace[i, j] == 0:
            i -= 1
            j -= 1
        elif trace[i, j] == 1:
            i -= 1
        elif trace[i, j] == 2:
            j -= 1
        else:
            raise ValueError("Unexpected trace[i, j]")
    result = np.array(result)
    return result[::-1, :].T


def _upstream_dtw_trace(x: np.ndarray) -> np.ndarray:
    N, M = x.shape
    cost = np.ones((N + 1, M + 1), dtype=np.float32) * np.inf
    trace = -np.ones((N + 1, M + 1), dtype=np.float32)
    cost[0, 0] = 0
    for j in range(1, M + 1):
        for i in range(1, N + 1):
            c0 = cost[i - 1, j - 1]
            c1 = cost[i - 1, j]
            c2 = cost[i, j - 1]
            if c0 < c1 and c0 < c2:
                c, t = c0, 0
            elif c1 < c0 and c1 < c2:
                c, t = c1, 1
            else:
                c, t = c2, 2
            cost[i, j] = x[i - 1, j - 1] + c
            trace[i, j] = t
    return trace


def _upstream_median_filter(x: np.ndarray, filter_width: int) -> np.ndarray:
    pad_width = filter_width // 2
    if x.shape[-1] <= pad_width:
        return x
    if (ndim := x.ndim) <= 2:
        x = x[None, None, :]
    x = np.pad(x, ((0, 0), (0, 0), (pad_width, pad_width)), mode="reflect").astype(np.float32)
    result = np.empty_like(x)
    for index in np.ndindex(x.shape[:-1]):
        row = [float(value) for value in x[index]]
        padded = [0.0] * pad_width + row + [0.0] * pad_width
        result[index] = [sorted(padded[k : k + filter_width])[pad_width] for k in range(len(row))]
    result = result[..., pad_width:-pad_width]
    if ndim <= 2:
        result = result[0, 0]
    return result


def _dtw_inputs():
    rng = np.random.default_rng(71)
    shapes = [(1, 1), (1, 9), (9, 1), (2, 2), (5, 7), (7, 40), (40, 7), (23, 61)]
    for shape in shapes:
        yield rng.integers(-2, 3, size=shape).astype(np.float32)  # many ties
        yield rng.integers(0, 2, size=shape).astype(np.float32)  # even more ties
        yield rng.standard_normal(shape).astype(np.float32)
        yield rng.standard_normal(shape)  # float64 input, float32 cost as upstream
    with_nan = rng.standard_normal((6, 11)).astype(np.float32)
    with_nan[2, 3] = np.nan
    yield with_nan


def test_dtw_gives_upstream_paths_and_traces_with_ties(vendored, monkeypatch) -> None:
    timing = vendored("timing")
    traces: list[np.ndarray] = []
    real_backtrace = timing.backtrace
    monkeypatch.setattr(
        timing, "backtrace", lambda trace: traces.append(trace.copy()) or real_backtrace(trace)
    )
    for x in _dtw_inputs():
        expected_trace = _upstream_dtw_trace(x)
        expected = _upstream_backtrace(expected_trace.copy())
        path = timing.dtw(x)
        assert path.dtype == expected.dtype and np.array_equal(path, expected), x.shape
        assert np.array_equal(traces[-1], expected_trace), x.shape


def test_dtw_is_fast_for_a_full_window(vendored) -> None:
    # About 450 tokens x 1500 audio frames; upstream needed numba for this.
    timing = vendored("timing")
    x = np.random.default_rng(3).standard_normal((450, 1500)).astype(np.float32)
    started = time.perf_counter()
    text_indices, time_indices = timing.dtw(x)
    elapsed = time.perf_counter() - started
    assert text_indices[-1] == 449 and time_indices[-1] == 1499
    assert elapsed < 1.0, elapsed  # about 40 ms; a Python loop over cells takes seconds


@pytest.mark.parametrize("width", [1, 3, 7])
def test_median_filter_gives_upstream_output(vendored, width) -> None:
    timing = vendored("timing")
    rng = np.random.default_rng(width)
    inputs = [
        rng.integers(-3, 4, size=(2, 5, 40)).astype(np.float32),  # ties
        rng.standard_normal((3, 4, 33)).astype(np.float32),
        rng.standard_normal((1, 2, 9)),  # float64 in, float32 out
        rng.integers(0, 3, size=25).astype(np.float32),  # 1-D
    ]
    for x in inputs:
        expected = _upstream_median_filter(x, width)
        result = timing.median_filter(x, width)
        assert result.dtype == expected.dtype and result.shape == expected.shape
        assert np.array_equal(result, expected)
    if width == 1:
        # Upstream crops [..., 0:-0]: width 1 gives an empty last axis, kept as is.
        assert timing.median_filter(inputs[0], 1).shape == (2, 5, 0)
    short = rng.standard_normal((2, 3, 3)).astype(np.float32)
    assert timing.median_filter(short, 7) is short  # too short to pad, returned unchanged


def test_median_filter_matches_scipy_where_it_is_installed(vendored) -> None:
    signal = pytest.importorskip("scipy.signal")
    timing = vendored("timing")
    x = np.random.default_rng(5).integers(-4, 5, size=(4, 6, 300)).astype(np.float32)
    padded = np.pad(x, ((0, 0), (0, 0), (3, 3)), mode="reflect")
    expected = signal.medfilt(padded, kernel_size=(1, 1, 7))[..., 3:-3]
    assert np.array_equal(timing.median_filter(x, 7), expected)


# ---------------------------------------------------------------- progress


def _decoded(*, silent: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        tokens=[1, 2, 3],
        temperature=0.0,
        avg_logprob=-2.0 if silent else -0.1,
        compression_ratio=1.0,
        no_speech_prob=0.9 if silent else 0.0,
    )


def _run_transcribe(module, monkeypatch, windows: list[SimpleNamespace], callback) -> dict:
    """The vendored transcribe() over 45 s of audio, with stand-ins for the model and MLX arrays."""
    results = iter(windows)

    class Model:
        dims = SimpleNamespace(n_mels=80, n_audio_ctx=1500)
        is_multilingual, num_languages = True, 99

        def decode(self, segment, options):
            return next(results)

    class Segment:
        def astype(self, dtype):
            return self

    monkeypatch.setattr(module.ModelHolder, "get_model", staticmethod(lambda path, dtype: Model()))
    # 45 s of audio (4500 frames) plus the 30 s of padding upstream appends.
    frames = module.N_FRAMES + 4500
    monkeypatch.setattr(module, "log_mel_spectrogram", lambda *a, **k: np.zeros((frames, 80), np.float32))
    monkeypatch.setattr(module, "pad_or_trim", lambda *a, **k: Segment())
    tokenizer = SimpleNamespace(eot=50257, timestamp_begin=50364, decode=lambda tokens: "x", encode=list)
    monkeypatch.setattr(module, "get_tokenizer", lambda *a, **k: tokenizer)
    return module.transcribe(
        "audio.flac", path_or_hf_repo=REPO, language="ja", verbose=False, progress_callback=callback
    )


def test_transcribe_reports_the_frames_tqdm_counted(vendored, monkeypatch) -> None:
    transcribe = vendored("transcribe")
    calls: list[tuple[int, int]] = []
    result = _run_transcribe(
        transcribe, monkeypatch, [_decoded(), _decoded()], lambda *done: calls.append(done)
    )
    # tqdm(total=4500), then update(3000) and update(1500).
    assert calls == [(0, 4500), (3000, 4500), (4500, 4500)]
    assert [segment["start"] for segment in result["segments"]] == [0.0, 30.0]

    # A silent window is skipped without an update, as with tqdm: the count ends below the total.
    calls.clear()
    _run_transcribe(
        transcribe, monkeypatch, [_decoded(silent=True), _decoded()], lambda *done: calls.append(done)
    )
    assert calls == [(0, 4500), (1500, 4500)]
    # Without a callback nothing is reported and nothing fails.
    assert _run_transcribe(transcribe, monkeypatch, [_decoded(), _decoded()], None)["language"] == "ja"


def test_worker_progress_file_keeps_its_format(tmp_path: Path, monkeypatch) -> None:
    def transcribe(audio, *, progress_callback, **options):
        for done in (0, 1500, 1500, 4500):
            progress_callback(done, 4500)
            seen.append(json.loads(progress.read_text(encoding="utf-8")))
        return {"segments": []}

    seen: list[dict] = []
    progress = tmp_path / "chunk.progress.json"
    fake = types.ModuleType(PACKAGE)
    fake.transcribe = transcribe
    monkeypatch.setitem(sys.modules, PACKAGE, fake)
    monkeypatch.delenv("PUDGE_BACKGROUND_PRIORITY", raising=False)
    assert stt_worker.main([str(tmp_path / "a.wav"), str(tmp_path / "out.json"), "model", str(progress)]) == 0
    assert [(item["percent"], item["current"], item["total"]) for item in seen] == [
        (0, 0, 4500),
        (33, 1500, 4500),
        (33, 1500, 4500),
        (100, 4500, 4500),
    ]
    assert all(set(item) == {"percent", "current", "total", "memory"} for item in seen)


# ---------------------------------------------------------------- availability


def test_the_package_imports_without_mlx_and_the_worker_reports_it_missing(without_mlx, capsys) -> None:
    package = importlib.import_module(PACKAGE)
    importlib.import_module(f"{PACKAGE}.hub")  # the downloader needs no MLX either
    with pytest.raises(ImportError):
        package.transcribe
    assert stt_worker.main(["--check"]) == 3
    assert "mlx and tiktoken" in capsys.readouterr().err


def test_worker_check_needs_mlx_and_tiktoken(vendored, monkeypatch) -> None:
    assert stt_worker.main(["--check"]) == 0
    from pudge.whisper_mlx import transcribe

    assert callable(transcribe) and transcribe.__name__ == "transcribe"
    _drop_fresh_imports()
    monkeypatch.setitem(sys.modules, "tiktoken", None)
    assert stt_worker.main(["--check"]) == 3


# ---------------------------------------------------------------- model download


@pytest.fixture
def cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    folder = tmp_path / "hf" / "hub"
    monkeypatch.setenv("HF_HUB_CACHE", str(folder))
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    return folder


def _no_network(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"unexpected request {request.url}")


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def _hub_layout(cache: Path, repo: str, files: dict[str, bytes]) -> Path:
    # As huggingface_hub writes it: blobs, snapshot links to them, refs/main.
    folder = cache / ("models--" + repo.replace("/", "--"))
    snapshot = folder / "snapshots" / COMMIT
    snapshot.mkdir(parents=True)
    (folder / "blobs").mkdir()
    for index, (name, data) in enumerate(files.items()):
        (folder / "blobs" / f"blob{index}").write_bytes(data)
        (snapshot / name).symlink_to(Path("..", "..", "blobs", f"blob{index}"))
    (folder / "refs").mkdir()
    (folder / "refs" / "main").write_text(COMMIT, encoding="utf-8")
    return snapshot


def test_a_model_already_in_the_hugging_face_cache_needs_no_network(cache: Path, monkeypatch) -> None:
    snapshot = _hub_layout(cache, REPO, {"config.json": b"{}", "weights.npz": b"w"})
    assert hub.snapshot_download(REPO, client=_client(_no_network)) == str(snapshot)
    # Also in offline mode, and for the precision model.
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    small = _hub_layout(
        cache, "mlx-community/whisper-small-mlx", {"config.json": b"{}", "weights.safetensors": b"w"}
    )
    assert hub.snapshot_download("mlx-community/whisper-small-mlx", client=_client(_no_network)) == str(small)


def test_cache_folder_follows_the_hugging_face_variables(tmp_path: Path, monkeypatch) -> None:
    for name in ("HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE", "HF_HOME", "XDG_CACHE_HOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert hub.hub_cache() == tmp_path / ".cache" / "huggingface" / "hub"
    monkeypatch.setenv("HF_HOME", str(tmp_path / "home"))
    assert hub.hub_cache() == tmp_path / "home" / "hub"
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "hub"))
    assert hub.hub_cache() == tmp_path / "hub"


def _hub_server(requests: list[str], *, files: list[str], weights: bytes = b"weights") -> object:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.host == "cdn.invalid":
            return httpx.Response(200, content=weights)
        if request.url.path == f"/api/models/{REPO}":
            siblings = [{"rfilename": name} for name in [".gitattributes", "README.md", *files]]
            return httpx.Response(200, json={"id": REPO, "sha": COMMIT, "siblings": siblings})
        prefix = f"/{REPO}/resolve/{COMMIT}/"
        if request.url.path == prefix + "config.json":
            return httpx.Response(200, json={"n_mels": 80})
        if request.url.path.startswith(prefix):
            # Large files are redirected to a CDN.
            return httpx.Response(302, headers={"Location": "https://cdn.invalid/blob/1"})
        return httpx.Response(404)

    return handler


def test_a_missing_model_is_downloaded_into_the_hugging_face_layout(cache: Path) -> None:
    requests: list[str] = []
    handler = _hub_server(requests, files=["config.json", "weights.npz", "model.safetensors"])
    path = Path(hub.snapshot_download(REPO, client=_client(handler)))
    folder = cache / "models--mlx-community--whisper-tiny"
    assert path == folder / "snapshots" / COMMIT
    assert (folder / "refs" / "main").read_text(encoding="utf-8") == COMMIT
    # Only what load_model reads, its preferred weights first.
    assert sorted(item.name for item in path.iterdir()) == ["config.json", "model.safetensors"]
    assert (path / "model.safetensors").read_bytes() == b"weights"
    assert json.loads((path / "config.json").read_text(encoding="utf-8")) == {"n_mels": 80}
    assert requests == [
        f"https://huggingface.co/api/models/{REPO}",
        f"https://huggingface.co/{REPO}/resolve/{COMMIT}/config.json",
        f"https://huggingface.co/{REPO}/resolve/{COMMIT}/model.safetensors",
        "https://cdn.invalid/blob/1",
    ]
    # The next start finds it without the network.
    assert hub.snapshot_download(REPO, client=_client(_no_network)) == str(path)


def test_the_app_client_follows_redirects_through_the_environment_proxy() -> None:
    client = hub._client()
    try:
        assert client.follow_redirects and client.headers["User-Agent"] == APP_SLUG
        assert client.timeout.connect == 10.0 and client.timeout.read == 60.0
        assert client.trust_env  # HTTPS_PROXY and friends, as for Pudge's other clients
    finally:
        client.close()


def test_the_owned_client_is_closed(cache: Path, monkeypatch) -> None:
    clients: list[httpx.Client] = []

    def make() -> httpx.Client:
        clients.append(_client(_hub_server([], files=["config.json", "weights.npz"])))
        return clients[-1]

    monkeypatch.setattr(hub, "_client", make)
    assert Path(hub.snapshot_download(REPO)).joinpath("weights.npz").read_bytes() == b"weights"
    assert clients[0].is_closed


def test_offline_mode_and_half_downloaded_snapshots(cache: Path, monkeypatch) -> None:
    # huggingface_hub links a file only once it is complete: without the weights link the
    # snapshot does not count.
    _hub_layout(cache, REPO, {"config.json": b"{}"})
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    with pytest.raises(hub.ModelDownloadError) as caught:
        hub.snapshot_download(REPO, client=_client(_no_network))
    assert str(caught.value) == i18n.err("stt.whisperModelNotDownloaded", model=REPO)


def test_a_failed_download_leaves_no_partial_files(cache: Path) -> None:
    def cut(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/weights.npz"):

            def body():
                yield b"part"
                raise httpx.ReadError("connection reset", request=request)

            return httpx.Response(200, content=body())
        return _hub_server([], files=["config.json", "weights.npz"])(request)

    with pytest.raises(hub.ModelDownloadError) as caught:
        hub.snapshot_download(REPO, client=_client(cut))
    assert str(caught.value).startswith(f"Could not download the Whisper model {REPO}: connection reset")
    folder = cache / "models--mlx-community--whisper-tiny"
    assert not (folder / "refs" / "main").exists()
    assert sorted(item.name for item in (folder / "snapshots" / COMMIT).iterdir()) == ["config.json"]
    # The next attempt fetches only what is missing.
    requests: list[str] = []
    hub.snapshot_download(REPO, client=_client(_hub_server(requests, files=["config.json", "weights.npz"])))
    assert not [url for url in requests if url.endswith("/config.json")]


def test_bad_names_and_repositories_without_mlx_weights(cache: Path) -> None:
    for name in ("../../etc", "whisper", "org/a--b", "org/../x"):
        with pytest.raises(hub.ModelDownloadError) as caught:
            hub.snapshot_download(name, client=_client(_no_network))
        assert str(caught.value) == i18n.err("stt.whisperModelName", model=name)
    with pytest.raises(hub.ModelDownloadError) as caught:
        hub.snapshot_download(
            REPO, client=_client(_hub_server([], files=["config.json", "pytorch_model.bin"]))
        )
    assert str(caught.value) == i18n.err("stt.whisperModelHasNoWeights", model=REPO)
    assert not (cache / "models--mlx-community--whisper-tiny").exists()


def test_a_failed_model_download_reaches_the_user_through_the_worker(
    cache: Path, tmp_path, monkeypatch, capsys
):
    # As load_model does: the worker's transcribe asks hub for the model first.
    def transcribe(audio, *, path_or_hf_repo, **options):
        hub.snapshot_download(path_or_hf_repo, client=_client(lambda request: httpx.Response(503)))

    fake = types.ModuleType(PACKAGE)
    fake.transcribe = transcribe
    monkeypatch.setitem(sys.modules, PACKAGE, fake)
    monkeypatch.delenv("PUDGE_BACKGROUND_PRIORITY", raising=False)
    assert stt_worker.main([str(tmp_path / "a.wav"), str(tmp_path / "out.json"), REPO]) == 4
    error = capsys.readouterr().err.strip().rsplit("; memory=", 1)[0]
    assert error.startswith(f"MLX Whisper failed: Could not download the Whisper model {REPO}: ")
    assert "503" in error
    # The window shows it in the interface language.
    assert i18n.message(error, "ru").startswith(
        f"Ошибка MLX Whisper: Не удалось скачать модель Whisper {REPO}: "
    )


# ---------------------------------------------------------------- packaging guard

FORBIDDEN_IMPORTS = {"torch", "numba", "scipy", "huggingface_hub", "tqdm", "more_itertools", "mlx_whisper"}
GONE_FROM_THE_LOCK = {
    "mlx-whisper",
    "torch",
    "numba",
    "llvmlite",
    "scipy",
    "huggingface-hub",
    "hf-xet",
    "sympy",
}


def test_vendored_whisper_needs_only_mlx_tiktoken_and_numpy() -> None:
    # policy: imports of the vendored package and the locked install, which cannot run on Linux CI.
    files = source_policy.sources("pudge/whisper_mlx/*.py")
    assert {Path(name).name for name in files} >= {
        "__init__.py",
        "audio.py",
        "decoding.py",
        "hub.py",
        "load_models.py",
        "timing.py",
        "tokenizer.py",
        "transcribe.py",
        "whisper.py",
    }
    found = set()
    for name, text in files.items():
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                modules = [node.module or ""]
            else:
                continue
            found |= {(name, module) for module in modules if module.split(".")[0] in FORBIDDEN_IMPORTS}
    assert found == set()

    packages = {package["name"] for package in tomllib.loads(source_policy.source("uv.lock"))["package"]}
    assert packages & GONE_FROM_THE_LOCK == set()
    assert {"mlx", "tiktoken", "numpy"} <= packages

    project = tomllib.loads(source_policy.source("pyproject.toml"))
    sync = project["project"]["optional-dependencies"]["sync"]
    assert [requirement.split(">=")[0] for requirement in sync] == ["ffsubsync", "mlx", "tiktoken"]
    # The wheel carries the license and the tokenizer/mel assets, and nothing upstream-only.
    package_data = project["tool"]["setuptools"]["package-data"]["pudge"]
    for shipped in (
        "LICENSE",
        "assets/gpt2.tiktoken",
        "assets/multilingual.tiktoken",
        "assets/mel_filters.npz",
    ):
        assert (ROOT / "pudge" / "whisper_mlx" / shipped).is_file(), shipped
        assert any(fnmatch.fnmatch(f"whisper_mlx/{shipped}", pattern) for pattern in package_data), shipped
    assert sorted(path.name for path in (ROOT / "pudge" / "whisper_mlx" / "assets").iterdir()) == [
        "gpt2.tiktoken",
        "mel_filters.npz",
        "multilingual.tiktoken",
    ]
