"""Running Pudge's macOS shell programs with stand-ins, on Linux and macOS (audit 71Б, g01).

install.sh, the in-app updater's generated script and build_release.sh are macOS programs.
`MacSim` runs them with the portable runner of t71g00_support (Linux and macOS alike): the
macOS tools they call are small stand-in programs that record their arguments and do the
minimum the script needs next, and zsh runs them in place of the real tools (a function per
name or absolute path in $ZDOTDIR/.zshenv; install.sh is a copy that looks for Homebrew and
qBittorrent under the fake root `MacSim.root`, where there are none):

  by absolute path   /usr/bin/ditto (copies), /usr/bin/clang (keeps the generated launcher
                     source, writes the executable), /usr/bin/open, /usr/libexec/PlistBuddy
                     (edits the plist), LaunchServices' lsregister
  by name            uname (Darwin), launchctl, codesign, sips / iconutil (write the icon
                     files), killall, python3.12; brew (formulas installed, --prefix) is on
                     PATH, a Homebrew outside the standard locations

The managed venv is a stand-in too: its `python` is the real interpreter with a private
site-packages (stdlib + that folder only, sysconfig pointing at the venv) and a pip that
behaves like pip where install.sh depends on it: a wheel of the version already installed
is "Requirement already satisfied", an install records its files, an uninstall removes the
recorded files. `pip wheel` packs the project's pudge/ like setuptools does, including its
quirk of reusing a build/lib copy that is not older than the source. `AppKit` in that
site-packages lists the apps given in MACSIM_RUNNING_BUNDLES and records terminate().

Every call lands in one log (`RunResult.calls(tool)`, `RunResult.index(tool, *args)`).
`release_history()` / `source_history()` run whole install histories once per test session
and keep what each step left behind. build_release.sh and the workflow steps need only bash
(`build_release()`, `load_workflow()` / `run_step()`, `Recorder`).

The module is also the stand-ins' program (`python t71g01_support.py venv-python ...`): the
stand-in part comes first and imports little, so the many stand-in calls start fast.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUPPORT = Path(__file__).resolve()
LSREGISTER = (
    "/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
)

# ---------------------------------------------------------------------------------------
# Stand-in programs (shell). RECORD appends one tab-separated line: tool, arguments.
RECORD = """#!/bin/sh
record() { line="$1"; shift; for arg in "$@"; do line="$line$(printf '\\t')$arg"; done; printf '%s\\n' "$line" >> "$MACSIM_LOG"; }
"""

PATH_TOOLS = {
    "uname": "echo Darwin\n",
    # Every formula is installed; no qBittorrent cask.
    "brew": 'record brew "$@"\ncase "$1" in\n  list) [ "$2" = "--versions" ] ;;\n  --prefix) printf \'%s\\n\' "$MACSIM_PREFIX/$2" ;;\nesac\n',
    "launchctl": 'record launchctl "$@"\n',
    "codesign": 'record codesign "$@"\n',
    "killall": 'record killall "$@"\n',
    "sips": 'record sips "$@"\nout=""; prev=""\nfor arg in "$@"; do [ "$prev" = "--out" ] && out="$arg"; prev="$arg"; done\nprintf \'png %s\\n\' "$4" > "$out"\n',
    "iconutil": 'record iconutil "$@"\nprintf \'icns %s\\n\' "$(ls "$3")" > "$5"\n',
    "python3.12": (
        'record python3.12 "$@"\n'
        'if [ "$1" = "-m" ] && [ "$2" = "venv" ]; then exec "$MACSIM_PYTHON" -I -S "$MACSIM_SUPPORT" make-venv "$3"; fi\n'
        'exec "$MACSIM_PYTHON" "$@"\n'
    ),
}

ROOT_TOOLS = {
    "/usr/bin/ditto": (
        'record ditto "$@"\n'
        'if [ -d "$1" ]; then mkdir -p "$2" && cp -pR "$1/." "$2/"; else mkdir -p "$(dirname "$2")" && cp -p "$1" "$2"; fi\n'
    ),
    "/usr/bin/clang": (
        'record clang "$@"\nout=""; source=""; prev=""\n'
        'for arg in "$@"; do [ "$prev" = "-o" ] && out="$arg"; case "$arg" in *.m) source="$arg";; esac; prev="$arg"; done\n'
        'cp "$source" "$MACSIM_DIR/launcher.m"\n'
        'if [ "${MACSIM_FAIL_CLANG:-0}" = 1 ]; then echo "clang: error: stand-in failure" >&2; exit 1; fi\n'
        "printf '#!/bin/sh\\n# native launcher (clang stand-in)\\n' > \"$out\"\n"
    ),
    "/usr/bin/open": 'record open "$@"\n',
    "/usr/libexec/PlistBuddy": 'record PlistBuddy "$@"\nexec "$MACSIM_PYTHON" -I -S "$MACSIM_SUPPORT" plistbuddy "$@"\n',
    LSREGISTER: 'record lsregister "$@"\n',
}

# What a fake venv contains besides python: python-config (embed flags only with --embed,
# like the real one) and the console scripts pip would create.
VENV_TOOLS = {
    "python3.12-config": (
        'record python-config "$@"\n'
        'case "$*" in\n'
        '  "--embed --cflags"|"--cflags") echo "-I$VENV/include/python3.12 -I$VENV/include/python3.12/internal" ;;\n'
        '  "--embed --ldflags") echo "-L$VENV/lib/python3.12/config -lpython3.12 -ldl -framework CoreFoundation" ;;\n'
        '  "--ldflags") echo "-L$VENV/lib/python3.12/config -ldl -framework CoreFoundation" ;;\n'
        "esac\n"
    ),
    "pudge": (
        'record pudge-cli "$@"\n'
        'if [ "$1" = "--init-config" ]; then mkdir -p "$HOME/.config/pudge" && echo "{}" > "$HOME/.config/pudge/config.toml"; fi\n'
    ),
    "pudge-agent": 'record pudge-agent "$@"\n',
}

APPKIT = '''"""AppKit stand-in: the running apps named in MACSIM_RUNNING_BUNDLES."""
import os


def _record(*fields):
    with open(os.environ["MACSIM_LOG"], "a", encoding="utf-8") as log:
        log.write("\\t".join(("appkit",) + fields) + "\\n")


class _App:
    def __init__(self, bundle):
        self.bundle, self.terminated = bundle, False

    def terminate(self):
        _record("terminate", self.bundle)
        self.terminated = True

    def isTerminated(self):
        return self.terminated


class NSRunningApplication:
    @staticmethod
    def runningApplicationsWithBundleIdentifier_(bundle):
        _record("lookup", bundle)
        running = os.environ.get("MACSIM_RUNNING_BUNDLES", "").split()
        return [_App(bundle)] if bundle in running else []
'''


def _write_tool(path: Path, body: str, *, env: dict[str, str] | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = RECORD + "".join(f"{key}={_sh(value)}\n" for key, value in (env or {}).items())
    path.write_text(header + body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _sh(value: str) -> str:
    return "'" + str(value).replace("'", "'\"'\"'") + "'"


def _record(*fields: str) -> None:
    with open(os.environ["MACSIM_LOG"], "a", encoding="utf-8") as log:
        log.write("\t".join(fields) + "\n")


# ---------------------------------------------------------------------------------------
# The managed venv stand-in.
def make_venv(venv: Path) -> None:
    """What `python3.12 -m venv VENV` leaves behind, as far as install.sh can tell."""
    site = venv / "lib" / "site"
    site.mkdir(parents=True, exist_ok=True)
    (site / "AppKit.py").write_text(APPKIT, encoding="utf-8")
    python = os.environ.get("MACSIM_PYTHON", sys.executable)
    _write_tool(
        venv / "bin" / "python",
        f'exec {_sh(python)} -I -S {_sh(str(SUPPORT))} venv-python {_sh(str(venv))} "$@"\n',
    )
    for name, body in VENV_TOOLS.items():
        _write_tool(venv / "bin" / name, body, env={"VENV": str(venv)})


def build_wheel(project: Path, out_dir: Path) -> Path:
    """`pip wheel PROJECT`: pudge/ packed as setuptools does it. setuptools' build_py keeps a
    build/lib copy that is not older than the source, so a stale build/ ends up in the wheel."""
    import tomllib
    import zipfile

    version = tomllib.loads((project / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    source, built = project / "pudge", project / "build" / "lib" / "pudge"
    files: dict[str, bytes] = {}
    for path in sorted(source.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        relative = path.relative_to(source)
        copy = built / relative
        reused = copy.is_file() and copy.stat().st_mtime >= path.stat().st_mtime
        files[f"pudge/{relative.as_posix()}"] = (copy if reused else path).read_bytes()
    info = f"pudge-{version}.dist-info"
    files[f"{info}/METADATA"] = f"Metadata-Version: 2.1\nName: pudge\nVersion: {version}\n".encode()
    files[f"{info}/RECORD"] = "".join(f"{name},,\n" for name in [*files, f"{info}/RECORD"]).encode()
    out_dir.mkdir(parents=True, exist_ok=True)
    wheel = out_dir / f"pudge-{version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return wheel


def _uninstall(site: Path, info: Path) -> None:
    import shutil

    for line in (info / "RECORD").read_text(encoding="utf-8").splitlines():
        target = site / line.split(",", 1)[0]
        if target.is_file():
            target.unlink()
    shutil.rmtree(info, ignore_errors=True)
    for folder in sorted(
        (p for p in site.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True
    ):
        if folder.name.startswith("pudge") or "pudge" in folder.relative_to(site).parts[:1]:
            try:
                folder.rmdir()
            except OSError:
                pass


def _install_wheel(site: Path, wheel: Path, *, force: bool) -> None:
    import zipfile

    version = wheel.name.split("-")[1]
    installed = sorted(site.glob("pudge-*.dist-info"))
    if installed and not force and installed[0].name == f"pudge-{version}.dist-info":
        print(f"Requirement already satisfied: pudge=={version}")
        return
    for info in installed:
        _uninstall(site, info)
    with zipfile.ZipFile(wheel) as archive:
        archive.extractall(site)
    for name in os.environ.get("MACSIM_PIP_LEAVES_STALE", "").split():
        (site / name).write_text("stale copy left behind\n", encoding="utf-8")


def fake_pip(venv: Path, args: list[str]) -> int:
    site = venv / "lib" / "site"
    _record("pip", *args)
    if args[:1] == ["wheel"]:
        rest = args[1:]
        out = Path(rest[rest.index("-w") + 1])
        project = Path(next(arg for arg in rest if not arg.startswith("-") and Path(arg) != out))
        build_info = project / "pudge" / "build-info.json"
        seen = {
            "build_info": json.loads(build_info.read_text(encoding="utf-8"))
            if build_info.is_file()
            else None,
            "project_build_present": (project / "build").exists(),
        }
        _record("pip-wheel-saw", json.dumps(seen))
        if os.environ.get("MACSIM_FAIL_PIP_WHEEL") == "1":
            print("error: stand-in wheel build failure", file=sys.stderr)
            return 1
        build_wheel(project, out)
        return 0
    if args[:1] == ["install"]:
        options = args[1:]
        if "-r" in options:
            return 0
        for target in (arg for arg in options if not arg.startswith("-")):
            path = target.split("[", 1)[0]
            if path.endswith(".whl"):
                _install_wheel(site, Path(path), force="--force-reinstall" in options)
            elif target.startswith("manga-ocr"):
                (site / "manga_ocr").mkdir(exist_ok=True)
                (site / "manga_ocr" / "__init__.py").write_text("", encoding="utf-8")
    return 0


def venv_python(venv: Path, args: list[str]) -> int:
    """The venv's python: `-m pip ...`, `[-I] - ARGS` (stdin), `[-I] -c CODE ARGS`, `[-I] SCRIPT ARGS`."""
    if args[:2] == ["-m", "pip"]:
        return fake_pip(venv, args[2:])
    rest = list(args)
    isolated = False
    while rest and rest[0] in ("-I", "-B", "-u", "-E", "-s"):
        isolated = isolated or rest[0] in ("-I", "-E")
        rest.pop(0)
    if rest[0] == "-c":
        code, filename, argv = rest[1], "<string>", ["-c", *rest[2:]]
    elif rest[0] == "-":
        code, filename, argv = sys.stdin.read(), "<stdin>", ["-", *rest[1:]]
    else:
        filename = rest[0]
        code, argv = Path(filename).read_text(encoding="utf-8"), list(rest)
    _record("venv-python", *rest)
    import sysconfig

    site = venv / "lib" / "site"
    stdlib = [
        entry
        for entry in sys.path
        if entry
        and "site-packages" not in entry
        and "dist-packages" not in entry
        and Path(entry).resolve() != SUPPORT.parent
    ]
    # Like a venv: PYTHONPATH (ignored by -I), the standard library, then site-packages.
    extra = (
        [] if isolated else [entry for entry in os.environ.get("PYTHONPATH", "").split(os.pathsep) if entry]
    )
    sys.path[:] = [*extra, *stdlib, str(site)]
    paths = dict(sysconfig.get_paths())
    paths["purelib"] = paths["platlib"] = str(site)
    config_var = sysconfig.get_config_var
    sysconfig.get_paths = lambda *_a, **_k: dict(paths)
    sysconfig.get_config_var = lambda name: {"BINDIR": str(venv / "bin"), "VERSION": "3.12"}.get(
        name, config_var(name)
    )
    sys.argv = argv
    sys.executable = str(venv / "bin" / "python")
    try:
        exec(compile(code, filename, "exec"), {"__name__": "__main__", "__file__": filename})
    except SystemExit as stop:
        if stop.code is None or isinstance(stop.code, int):
            return int(stop.code or 0)
        print(stop.code, file=sys.stderr)
        return 1
    return 0


def plistbuddy(args: list[str]) -> int:
    """`PlistBuddy -c "Add :Key type value" FILE`."""
    import plistlib

    command, target = args[args.index("-c") + 1], Path(args[-1])
    verb, key, kind, value = command.split(" ", 3)
    data = plistlib.loads(target.read_bytes())
    if verb != "Add" or kind != "string":
        return 1
    data[key.lstrip(":")] = value
    target.write_bytes(plistlib.dumps(data))
    return 0


def release_pip(args: list[str]) -> int:
    """`python -m pip wheel . --no-deps --no-build-isolation -w dist` in build_release.sh."""
    _record("pip", *args)
    key = Path("pudge/assets/jimaku-trial-key")
    _record(
        "pip-wheel-saw", json.dumps({"trial_key": key.read_text(encoding="utf-8") if key.is_file() else None})
    )
    build_wheel(Path.cwd(), Path(args[args.index("-w") + 1]))
    return 0


def _main(argv: list[str]) -> int:
    command, rest = argv[0], argv[1:]
    if command == "make-venv":
        make_venv(Path(rest[0]))
        return 0
    if command == "venv-python":
        return venv_python(Path(rest[0]), rest[1:])
    if command == "plistbuddy":
        return plistbuddy(rest)
    if command == "release-pip":
        return release_pip(rest)
    raise SystemExit(f"unknown stand-in command {command}")


if __name__ == "__main__":  # a stand-in program: no test-side imports needed
    raise SystemExit(_main(sys.argv[1:]))

import atexit  # noqa: E402 - the test side (below) imports the rest
import functools  # noqa: E402
import plistlib  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402
import zipfile  # noqa: E402
from dataclasses import dataclass, field  # noqa: E402

from t71g00_support import SYSTEM_PATH, copy_script, mac_env  # noqa: E402


# ---------------------------------------------------------------------------------------
# The stand-in Mac.
@dataclass(repr=False)
class RunResult:
    completed: subprocess.CompletedProcess[str]
    events: list[list[str]]

    def __repr__(self) -> str:
        return f"<run: exit {self.completed.returncode}, {len(self.events)} recorded calls>"

    @property
    def returncode(self) -> int:
        return self.completed.returncode

    @property
    def output(self) -> str:
        return self.completed.stdout + self.completed.stderr

    def calls(self, tool: str) -> list[list[str]]:
        """Arguments of every call of a stand-in during this run, in order."""
        return [event[1:] for event in self.events if event[0] == tool]

    def index(self, tool: str, *prefix: str) -> int:
        """Position of the first call of `tool` whose arguments start with `prefix`."""
        for position, event in enumerate(self.events):
            if event[0] == tool and event[1 : 1 + len(prefix)] == list(prefix):
                return position
        raise AssertionError(f"no call {tool} {' '.join(prefix)} in {self.events}")


@dataclass
class MacSim:
    """A fake Mac user (HOME, Homebrew prefix, stand-in tools) under `base`."""

    base: Path
    env: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if shutil.which("zsh") is None:
            import pytest

            pytest.skip("zsh is not installed")
        self.base.mkdir(parents=True, exist_ok=True)
        self.home = self.base / "home"
        self.bin = self.base / "bin"
        self.root = self.base / "root"
        self.log = self.base / "calls.log"
        self.prefix = self.base / "homebrew"
        self.tmp = self.base / "tmp"
        for folder in (self.home, self.tmp):
            folder.mkdir(parents=True, exist_ok=True)
        self.log.touch()
        programs = {name: _write_tool(self.bin / name, body) for name, body in PATH_TOOLS.items()}
        programs.update(
            {path: _write_tool(self.root / path.lstrip("/"), body) for path, body in ROOT_TOOLS.items()}
        )
        for tool in (
            "mpv/bin/mpv",
            "ffmpeg/bin/ffmpeg",
            "ffmpeg/bin/ffprobe",
            "alass/bin/alass-cli",
            "sevenzip/bin/7zz",
            "aria2/bin/aria2c",
        ):
            _write_tool(self.prefix / tool, "exit 0\n")
        # zsh runs the stand-ins for these names and paths; brew is found on PATH.
        standins = "".join(
            f'{name}() {{ {_sh(str(program))} "$@"; }}\n'
            for name, program in programs.items()
            if name != "brew"
        )
        self.env = mac_env(
            self.base,
            standins,
            root=self.root,
            home=self.home,
            path=f"{self.bin}:{SYSTEM_PATH}",
            TMPDIR=str(self.tmp),
            MACSIM_LOG=str(self.log),
            MACSIM_DIR=str(self.base),
            MACSIM_PREFIX=str(self.prefix),
            MACSIM_PYTHON=sys.executable,
            MACSIM_SUPPORT=str(SUPPORT),
        )

    # Paths of the installed Pudge (brand.env: APP_NAME=pudge, APP_SLUG=pudge).
    @property
    def data(self) -> Path:
        return self.home / ".local/share/pudge"

    @property
    def venv(self) -> Path:
        return self.data / "venv"

    @property
    def site(self) -> Path:
        return self.venv / "lib" / "site"

    @property
    def app(self) -> Path:
        return self.home / "Applications/pudge.app"

    @property
    def config(self) -> Path:
        return self.home / ".config/pudge/config.toml"

    @property
    def agent_plist(self) -> Path:
        return self.home / "Library/LaunchAgents/com.pudge.agent.plist"

    @property
    def launcher_source(self) -> Path:
        """The launcher source install.sh generated (kept by the clang stand-in)."""
        return self.base / "launcher.m"

    def launcher(self) -> str:
        """Text of the last launcher install.sh generated."""
        return self.launcher_source.read_text(encoding="utf-8")

    def run(
        self,
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str] | None = None,
        stdin: str | None = None,
        timeout: float = 120,
    ) -> RunResult:
        start = len(self.log.read_text(encoding="utf-8").splitlines())
        completed = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            env={**self.env, **(env or {})},
            input=stdin,
            timeout=timeout,
        )
        lines = self.log.read_text(encoding="utf-8").splitlines()[start:]
        return RunResult(completed, [line.split("\t") for line in lines])

    # A Pudge checkout or release folder to install from.
    def project(
        self, name: str, *, version: str = "9.8.7", kind: str = "release", files: dict[str, str] | None = None
    ) -> Path:
        """kind: "release" (a release ZIP: the wheel + hashed requirements, no .git),
        "git" (a development checkout), "tree" (sources without .git or wheel)."""
        project = self.base / name
        package = project / "pudge"
        (package / "assets").mkdir(parents=True)
        (project / ".github/release").mkdir(parents=True)
        copy_script(ROOT / "install.sh", project / "install.sh")
        shutil.copy2(ROOT / "pudge/brand.env", package / "brand.env")
        shutil.copy2(
            ROOT / ".github/release/write_build_info.py", project / ".github/release/write_build_info.py"
        )
        (project / "pyproject.toml").write_text(
            f'[project]\nname = "pudge"\nversion = "{version}"\n', encoding="utf-8"
        )
        (package / "assets/app-icon.png").write_bytes(b"\x89PNG icon")
        sources = {
            "__init__.py": f'__version__ = "{version}"\n',
            "config.py": FIXTURE_CONFIG,
            "install_checks.py": "",
            "legacy_install.py": "",
            "mac_app_aliases.py": "",
            "web/app.js": f"// Pudge {version}\n",
            **(files or {}),
        }
        for relative, text in sources.items():
            (package / relative).parent.mkdir(parents=True, exist_ok=True)
            (package / relative).write_text(text, encoding="utf-8")
        if kind == "release":
            build_wheel(project, project)
            (project / "release-requirements.txt").write_text(
                "httpx==0.28.1 --hash=sha256:00\n", encoding="utf-8"
            )
        elif kind == "git":
            git = [
                "git",
                "-C",
                str(project),
                "-c",
                "user.name=Pudge Tests",
                "-c",
                "user.email=tests@example.invalid",
            ]
            subprocess.run(["git", "init", "-q", str(project)], check=True)
            subprocess.run([*git, "add", "-A"], check=True)
            subprocess.run([*git, "commit", "-qm", "initial"], check=True)
        return project

    def install(self, project: Path, *args: str, env: dict[str, str] | None = None) -> RunResult:
        """Runs `zsh install.sh ARGS` from the project folder."""
        return self.run(
            [shutil.which("zsh") or "/bin/zsh", str(project / "install.sh"), *args], cwd=project, env=env
        )

    def runtime(self) -> dict[str, bytes]:
        """The installed Pudge in the venv (pudge/ and its dist-info): {path: bytes}."""
        return {
            name: data for name, data in tree(self.site).items() if name.split("/")[0].startswith("pudge")
        }

    def records(self) -> dict[str, str]:
        """install.sh's records in the data folder."""
        return {
            name: (self.data / name).read_text(encoding="utf-8")
            for name in ("install-source.json", "installed-version.txt", "native-shell-rev")
            if (self.data / name).is_file()
        }


# The fixture's pudge.config: just what install.sh's inline Python uses.
FIXTURE_CONFIG = """import json
from pathlib import Path
from types import SimpleNamespace


def load_config(path=None):
    data = json.loads(Path(path).read_text() or "{}") if path and Path(path).is_file() else {}
    return SimpleNamespace(tools=SimpleNamespace(**data.get("tools", {})), aria2=SimpleNamespace(**data.get("aria2", {})),
                           paths=SimpleNamespace(cache_dir=data.get("cache_dir", str(Path(path).parent / "cache"))))


def write_config(config, path):
    Path(path).write_text(json.dumps({"tools": vars(config.tools), "aria2": vars(config.aria2),
                                      "cache_dir": str(config.paths.cache_dir)}))
"""


# ---------------------------------------------------------------------------------------
# Install histories shared by the installer tests (each runs once per test session).
def _workspace(name: str) -> Path:
    base = Path(tempfile.mkdtemp(prefix=f"t71g01-{name}-")).resolve()  # as install.sh sees it (${0:A})
    atexit.register(shutil.rmtree, base, True)
    return base


def tree(root: Path) -> dict[str, bytes]:
    """{relative path: bytes} of the files under root ({} when it does not exist)."""
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts
    }


@dataclass(repr=False)
class Step:
    """One installer run and the installed app bundle / runtime package around it."""

    result: RunResult
    app_before: dict[str, bytes]
    runtime_before: dict[str, bytes]
    app_after: dict[str, bytes]
    runtime_after: dict[str, bytes]
    records: dict[str, str]

    def __repr__(self) -> str:
        return f"<install step: {self.result!r}>"


def _step(sim: MacSim, project: Path, *args: str, env: dict[str, str] | None = None) -> Step:
    app_before, runtime_before = tree(sim.app), sim.runtime()
    result = sim.install(project, *args, env=env)
    return Step(result, app_before, runtime_before, tree(sim.app), sim.runtime(), sim.records())


@dataclass
class History:
    sim: MacSim
    projects: dict[str, Path]
    steps: dict[str, Step]


@functools.cache
def release_history() -> History:
    """A release user's history: a fresh install of 9.8.6 (an old-brand app and agent are still
    there), three failed attempts to update to 9.8.7, a successful in-app update to a patched
    9.8.7 (same version number, changed files)."""
    sim = MacSim(_workspace("release"))
    (sim.home / "Applications/Anime MPV.app/Contents").mkdir(parents=True)
    legacy_agent = sim.home / "Library/LaunchAgents/com.anime-mpv.agent.plist"
    legacy_agent.parent.mkdir(parents=True)
    legacy_agent.write_text("<plist/>", encoding="utf-8")
    projects = {
        "9.8.6": sim.project("pudge-9.8.6", version="9.8.6"),
        "9.8.7": sim.project("pudge-9.8.7", version="9.8.7", files={"old_module.py": "OLD = True\n"}),
        "9.8.7-patched": sim.project(
            "pudge-9.8.7-patched", version="9.8.7", files={"web/app.js": "// Pudge 9.8.7 patched\n"}
        ),
    }
    steps = {"install": _step(sim, projects["9.8.6"])}
    # pip leaves an old copy of a file behind.
    steps["stale"] = _step(
        sim, projects["9.8.7"], "--update", env={"MACSIM_PIP_LEAVES_STALE": "pudge/web/app.js"}
    )
    # The native app has to be rebuilt and the build fails.
    steps["build_fails"] = _step(
        sim, projects["9.8.7"], "--update", env={"PUDGE_FORCE_NATIVE_REBUILD": "1", "MACSIM_FAIL_CLANG": "1"}
    )
    # A full install fails after the new app bundle replaced the old one.
    (sim.site / "manga_ocr").mkdir()
    (sim.site / "manga_ocr/__init__.py").write_text("", encoding="utf-8")
    blocker = sim.data / "install-source.json"
    blocker.unlink()
    blocker.mkdir()
    steps["after_swap_fails"] = _step(sim, projects["9.8.7"])
    blocker.rmdir()
    # The in-app updater installs a patched build while Pudge is still running.
    steps["update"] = _step(
        sim, projects["9.8.7-patched"], "--update", env={"MACSIM_RUNNING_BUNDLES": "com.pudge.app"}
    )
    return History(sim, projects, steps)


@functools.cache
def source_history() -> History:
    """A developer's history: installs from a dirty git checkout with a stale build/ folder,
    the same with a failing wheel build, a patched release folder built on request
    (PUDGE_BUILD_CURRENT_TREE=1), and a tree whose code does not match its version while
    PYTHONPATH points at a matching copy."""
    sim = MacSim(_workspace("source"))
    checkout = sim.project("checkout", version="9.8.7", kind="git")
    (checkout / "release-requirements.txt").write_text("httpx==0.28.1 --hash=sha256:00\n", encoding="utf-8")
    source = checkout / "pudge/web/app.js"
    source.write_text("// Pudge 9.8.7 work in progress\n", encoding="utf-8")
    stale = checkout / "build/lib/pudge/web/app.js"
    stale.parent.mkdir(parents=True)
    stale.write_text("// stale build output\n", encoding="utf-8")
    os.utime(stale, (source.stat().st_mtime + 100, source.stat().st_mtime + 100))
    projects = {"checkout": checkout}
    steps = {"install": _step(sim, checkout)}
    steps["wheel_fails"] = _step(sim, checkout, env={"MACSIM_FAIL_PIP_WHEEL": "1"})
    patched = projects["patched"] = sim.project("pudge-9.8.8", version="9.8.8")
    (patched / "pudge/web/app.js").write_text("// Pudge 9.8.8 + patch\n", encoding="utf-8")
    steps["patched"] = _step(sim, patched, env={"PUDGE_BUILD_CURRENT_TREE": "1"})
    mismatch = projects["mismatch"] = sim.project(
        "pudge-9.9.0", version="9.9.0", kind="tree", files={"__init__.py": '__version__ = "9.8.9"\n'}
    )
    elsewhere = sim.base / "elsewhere"
    (elsewhere / "pudge").mkdir(parents=True)
    (elsewhere / "pudge/__init__.py").write_text('__version__ = "9.9.0"\n', encoding="utf-8")
    (elsewhere / "pudge/config.py").write_text(FIXTURE_CONFIG, encoding="utf-8")
    (elsewhere / "pudge-9.9.0.dist-info").mkdir()
    (elsewhere / "pudge-9.9.0.dist-info/METADATA").write_text(
        "Metadata-Version: 2.1\nName: pudge\nVersion: 9.9.0\n", encoding="utf-8"
    )
    steps["mismatch"] = _step(
        sim, mismatch, env={"PUDGE_BUILD_CURRENT_TREE": "1", "PYTHONPATH": str(elsewhere)}
    )
    return History(sim, projects, steps)


# The audiobooks page: Wolf volumes 1 (id 3: cover, AniList link, a bookmark) and 2 (id 4: cover
# without a link) share a
# series card; "Other" (id 5) stands alone. page.run(api=AUDIOBOOKS); AUDIO_SELECTION starts a
# run body (opens the page; selected() / card(id)).
AUDIOBOOKS = {
    "audiobook_state": {
        "books": [
            {
                "id": 3,
                "title": "Book 1",
                "series_key": "wolf",
                "series_title": "Wolf",
                "volume": 1,
                "duration": 600,
                "position": 120,
                "cover_url": "/covers/a.jpg",
                "anilist_id": 9,
                "bookmarks": [{"id": 9, "position": 60, "title": "Here"}],
            },
            {
                "id": 4,
                "title": "Book 2",
                "series_key": "wolf",
                "series_title": "Wolf",
                "volume": 2,
                "duration": 600,
                "position": 0,
                "cover_url": "/covers/b.jpg",
            },
            {"id": 5, "title": "Other", "duration": 300, "position": 0},
        ]
    }
}
AUDIO_SELECTION = r"""
setPage('audiobooks'); await __tick(300);
const selected = () => [...document.querySelectorAll('#audiobooksContent .audiobook-card.selected')].map(c => Number(c.dataset.audiobookId));
const card = id => `#audiobooksContent .audiobook-card[data-audiobook-id="${id}"]`;
"""


def launcher_main(sim: MacSim) -> str:
    """The body of main() in the launcher install.sh generated."""
    text = sim.launcher()
    return text[text.index("int main(int argc, char *argv[])") :]


# ---------------------------------------------------------------------------------------
# build_release.sh in a small project, with stand-ins for `python -m pip wheel` and `uv`
# (pytest is skipped as in CI: SKIP_TESTS=1). Runs anywhere bash runs.
RELEASE_TOOLS = {
    "python": (
        'record python "$@"\n'
        'if [ "$1" = "-m" ] && [ "$2" = "pip" ]; then shift 2; exec "$MACSIM_PYTHON" -I -S "$MACSIM_SUPPORT" release-pip "$@"; fi\n'
        'if [ "$1" = "-m" ] && [ "$2" = "pytest" ]; then exit 0; fi\n'
        'exec "$MACSIM_PYTHON" "$@"\n'
    ),
    "uv": (
        'record uv "$@"\nprev=""\n'
        'for arg in "$@"; do [ "$prev" = "--output-file" ] && printf \'httpx==0.28.1 --hash=sha256:00\\n\' > "$arg"; prev="$arg"; done\n'
    ),
}
RELEASE_FILES = (
    "install.sh",
    "README.md",
    "config.example.toml",
    "pyproject.toml",
    "Makefile",
    "LICENSE",
    "SECURITY.md",
    "CONTRIBUTING.md",
    "CHANGELOG.md",
    "uv.lock",
)


@dataclass
class ReleaseBuild:
    project: Path
    result: RunResult

    @property
    def archive(self) -> Path:
        return self.project / "dist/pudge-macos-v9.8.7.zip"

    def shipped(self) -> dict[str, zipfile.ZipInfo]:
        """{path inside the release folder: zip entry} of the release ZIP."""
        with zipfile.ZipFile(self.archive) as archive:
            return {
                info.filename.split("/", 1)[1]: info
                for info in archive.infolist()
                if not info.is_dir() and "/" in info.filename
            }


def build_release(base: Path, *, env: dict[str, str] | None = None, readme: str = "") -> ReleaseBuild:
    """Runs build_release.sh in a small Pudge project (version 9.8.7) under `base`."""
    project, tools, log = base / "project", base / "bin", base / "calls.log"
    (project / "pudge/assets").mkdir(parents=True)
    (project / "pudge/__pycache__").mkdir()
    (project / "tests").mkdir()
    (project / ".github/workflows").mkdir(parents=True)
    (project / ".github/release").mkdir()
    log.touch()
    shutil.copy2(ROOT / "build_release.sh", project / "build_release.sh")
    shutil.copy2(ROOT / "pudge/brand.env", project / "pudge/brand.env")
    for script in ("write_build_info.py", "check_release_bundle.py", "finish_release_archives.py"):
        shutil.copy2(ROOT / ".github/release" / script, project / ".github/release" / script)
    files = {
        "pudge/__init__.py": '__version__ = "9.8.7"\n',
        "pudge/legacy_install.py": "",
        "pudge/install_checks.py": "",
        "pudge/__pycache__/cli.cpython-312.pyc": "",
        "tests/test_example.py": "def test_ok():\n    pass\n",
        ".github/workflows/release.yml": "name: Release\n",
        ".github/workflows/ci.yml": "name: CI\n",
        "MOBILE_SYNC_PROTOCOL.md": "# Mobile sync\n",
        "rename_brand.py": "",
        "brand_migration.py": "",
        **{name: "" for name in RELEASE_FILES},
        "pyproject.toml": '[project]\nname = "pudge"\nversion = "9.8.7"\n',
        "README.md": readme
        or "See [the Makefile](Makefile), [tests](tests/), [release tooling](.github/release/check_release_bundle.py)"
        " and [the release workflow](.github/workflows/release.yml).\n",
    }
    for relative, text in files.items():
        (project / relative).write_text(text, encoding="utf-8")
    for name, body in RELEASE_TOOLS.items():
        _write_tool(tools / name, body)
    completed = subprocess.run(
        ["bash", str(project / "build_release.sh")],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=120,
        env={
            "HOME": str(base),
            "PATH": f"{tools}:/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "SKIP_TESTS": "1",
            "MACSIM_LOG": str(log),
            "MACSIM_PYTHON": sys.executable,
            "MACSIM_SUPPORT": str(SUPPORT),
            **(env or {}),
        },
    )
    events = [line.split("\t") for line in log.read_text(encoding="utf-8").splitlines()]
    return ReleaseBuild(project, RunResult(completed, events))


# ---------------------------------------------------------------------------------------
# GitHub workflows: a reader for the YAML they use (block mappings and sequences, literal
# blocks, flow lists, quoted scalars) and a runner for one step's shell script.
def _scalar(text: str):
    text = text.strip()
    if text.startswith("[") and text.endswith("]"):
        return [_scalar(item) for item in text[1:-1].split(",") if item.strip()]
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text


def _key_value(text: str) -> tuple[str, str]:
    if text.endswith(":"):
        return text[:-1].strip(), ""
    key, value = text.split(": ", 1)
    return key.strip(), value.strip()


def load_workflow(text: str):
    lines = text.splitlines()

    def significant(index: int) -> int:
        while index < len(lines) and (not lines[index].strip() or lines[index].lstrip().startswith("#")):
            index += 1
        return index

    def indent_of(index: int) -> int:
        return len(lines[index]) - len(lines[index].lstrip())

    def block_scalar(index: int, parent: int) -> tuple[str, int]:
        body: list[str] = []
        while index < len(lines) and (not lines[index].strip() or indent_of(index) > parent):
            body.append(lines[index])
            index += 1
        while body and not body[-1].strip():
            body.pop()
        margin = min(len(line) - len(line.lstrip()) for line in body if line.strip())
        return "\n".join(line[margin:] for line in body) + "\n", index

    def value_after(rest: str, index: int, indent: int):
        if rest.startswith("|"):
            return block_scalar(index, indent)
        if rest:
            return _scalar(rest), index
        nxt = significant(index)
        if nxt < len(lines) and indent_of(nxt) > indent:
            return parse(nxt, indent_of(nxt))
        return None, index

    def parse(index: int, indent: int, first: str | None = None):
        index = significant(index) if first is None else index
        text = first if first is not None else lines[index].strip()
        if text.startswith("- "):
            items = []
            while True:
                item = text[2:]
                if ": " in item or item.endswith(":"):
                    value, index = parse(index, indent + 2, first=item)
                else:
                    value, index = _scalar(item), index + 1
                items.append(value)
                index = significant(index)
                if (
                    index >= len(lines)
                    or indent_of(index) != indent
                    or not lines[index].strip().startswith("- ")
                ):
                    return items, index
                text = lines[index].strip()
        mapping = {}
        while True:
            key, rest = _key_value(text)
            mapping[key], index = value_after(rest, index + 1, indent)
            index = significant(index)
            if index >= len(lines) or indent_of(index) != indent or lines[index].strip().startswith("- "):
                return mapping, index
            text = lines[index].strip()

    return parse(0, 0)[0]


def workflow_step(workflow: dict, job: str, name: str) -> dict:
    return next(step for step in workflow["jobs"][job]["steps"] if step.get("name") == name)


def run_step(
    step: dict,
    cwd: Path,
    *,
    tools: Path,
    expressions: dict[str, str] | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Runs a step's `run` script as GitHub does (bash -eo pipefail) with its `env`; each
    `${{ expression }}` in that env must be given in `expressions` (missing secrets are "")."""
    import re

    expressions = expressions or {}

    def evaluate(value: str) -> str:
        def one(match: re.Match[str]) -> str:
            expression = match.group(1).strip()
            if expression in expressions:
                return expressions[expression]
            if expression.startswith("secrets."):
                return ""
            raise AssertionError(f"no value for ${{{{ {expression} }}}}")

        return re.sub(r"\$\{\{(.*?)\}\}", one, str(value))

    step_env = {key: evaluate(value) for key, value in (step.get("env") or {}).items()}
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", step["run"]],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=60,
        env={
            "HOME": str(cwd),
            "PATH": f"{tools}:/usr/bin:/bin",
            "LANG": "C.UTF-8",
            **step_env,
            **(env or {}),
        },
    )


class Recorder:
    """Stand-in programs in base/bin that record their calls (tool, arguments) in base/calls.log."""

    def __init__(self, base: Path) -> None:
        self.base, self.bin, self.log = base, base / "bin", base / "calls.log"
        self.bin.mkdir(parents=True, exist_ok=True)
        self.log.touch()

    def tool(self, name: str, body: str = "") -> Path:
        """Program `name` on PATH: records its arguments, then runs `body` (sh)."""
        return _write_tool(self.bin / name, f'record {name} "$@"\n{body}')

    @property
    def env(self) -> dict[str, str]:
        return {"MACSIM_LOG": str(self.log)}

    def calls(self, tool: str | None = None) -> list[list[str]]:
        rows = [line.split("\t") for line in self.log.read_text(encoding="utf-8").splitlines()]
        return [row[1:] for row in rows if row[0] == tool] if tool else rows


def stand_in(path: Path, body: str) -> Path:
    """A stand-in program at `path` (RECORD's `record TOOL ARGS...` is available in `body`)."""
    return _write_tool(path, body)


def wheel_files(wheel: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(wheel) as archive:
        return {name: archive.read(name) for name in archive.namelist() if not name.endswith("/")}


def launch_update(sim: MacSim, project: Path, monkeypatch) -> list[str]:
    """Lets pudge.updater write its update script for `project` as if Pudge (an already exited
    process) asked for it; returns the command it would start."""
    from types import SimpleNamespace

    from pudge.updater import AppUpdater

    gone = subprocess.Popen(["true"])
    gone.wait()
    launched: list[list[str]] = []
    updater = AppUpdater()
    updater.update_root, updater.log_path = sim.base / "updates", sim.base / "update.log"
    with monkeypatch.context() as patch:
        patch.setattr("pudge.updater.Path.home", lambda: sim.home)
        patch.setattr("pudge.updater.os.getpid", lambda: gone.pid)
        patch.setattr(
            "pudge.updater.subprocess.Popen",
            lambda command, **_kw: launched.append(command) or SimpleNamespace(pid=0),
        )
        updater._launch_script(project, [])
    return launched[0]


def plist(path: Path) -> dict:
    return plistlib.loads(path.read_bytes())
