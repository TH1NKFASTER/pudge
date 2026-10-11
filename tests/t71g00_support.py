"""Runs install.sh end to end with stand-ins for macOS (audit 71Б).

The portable runner (shared with t71g01_support.py and test_v0711_gui_homebrew_path.py):
Pudge's zsh programs (install.sh, the updater's generated script) run under the real zsh, on
Linux and on a Mac alike, with every macOS tool a stand-in defined as a zsh function in
$ZDOTDIR/.zshenv, which every zsh reads (zsh runs functions even for names with slashes, such
as /usr/bin/clang or "$LSREGISTER"). `mac_env()` writes that .zshenv: first GUARD (a macOS tool
without a stand-in fails instead of running, the real Homebrew is never on PATH), then the
test's stand-ins, which fail like programs do (AS_PROGRAMS). What a script looks at on disk (`[[ -x /opt/homebrew/bin/brew ]]`) cannot
be a function: `copy_script()` gives the test a copy of install.sh in which exactly the PROBED
locations are looked up under the test's fake root ($MAC_ROOT) instead.

`run_install(tmp_path, ...)` copies install.sh and the files it uses into a project folder
in tmp_path and runs it under zsh with HOME in tmp_path. Everything macOS-only is a stand-in
defined as a zsh function:

* `brew` answers `list --versions` for the formulas given as installed, records installs and
  reports tool folders under a fake prefix; `uname` says Darwin.
* `python3.12` runs the installer's own Python (heredocs, legacy_install.py) with the test's
  interpreter; `install_checks.py` only records (its result is `checks_status`), and
  `-m venv` makes a real venv whose `bin/python` is a recording wrapper: `-m pip` is emulated
  (`pip wheel` zips the project's pudge/ folder, `pip install --no-deps X.whl` unpacks it into
  the venv's site-packages, manga-ocr installs an empty module), `mac_app_aliases.py` only
  records, and other calls run the venv's real Python with a stand-in `AppKit` (the running
  apps in `running_apps`; its clock runs fast so the 30-second quit deadline passes at once)
  and a `python3.X-config` that records.
* /usr/bin/clang records its arguments and writes a placeholder launcher; sips, iconutil,
  PlistBuddy, lsregister, launchctl, codesign, killall, pkill and git record; ditto copies.

The installed package is the project's pudge/__init__.py (the wheel's version) whose package
path also reaches the repository's pudge/ modules, so the installer's config steps run the
real pudge.config. Every stand-in call is recorded: `result.calls` lists [name, *args].

`build_launcher(source, folder)` compiles the Objective-C launcher the installer wrote against
small stand-ins for Foundation/AppKit/UserNotifications on gcc's Objective-C runtime and this
Python, so the launcher itself can run (Linux with clang and libobjc; skipped elsewhere). The
stand-ins write what the launcher does (autorelease pools, notifications) to $FAKE_TRACE.
"""

from __future__ import annotations

import json
import plistlib
import shutil
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
ZSH = shutil.which("zsh")
SEP = "\x1f"
LSREGISTER = (
    "/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
)
FORMULAS = ("mpv", "ffmpeg", "alass", "sevenzip", "aria2", "python@3.12", "python-tk@3.12")
TOOLS = (
    "mpv/bin/mpv",
    "ffmpeg/bin/ffmpeg",
    "ffmpeg/bin/ffprobe",
    "alass/bin/alass-cli",
    "sevenzip/bin/7zz",
    "aria2/bin/aria2c",
)
PY_VERSION = f"{sys.version_info.major}.{sys.version_info.minor}"
FAKE_REVISION = "0123456789abcdef0123456789abcdef01234567"

# ---------------------------------------------------------------------------------------
# The portable runner.
SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
HOMEBREW = ("/opt/homebrew/bin/brew", "/usr/local/bin/brew")
# Locations install.sh tests on disk; the test's copy looks for them under $MAC_ROOT.
PROBED = (*HOMEBREW, "/Applications/qbittorrent.app", "/Applications/qBittorrent.app")
# macOS tools that must never run for real from a test (the scripts' own stand-ins replace them).
GUARDED = (
    "launchctl",
    "codesign",
    "killall",
    "pkill",
    "sips",
    "iconutil",
    "open",
    "osascript",
    "/usr/bin/open",
    "/usr/bin/ditto",
    "/usr/bin/clang",
    "/usr/libexec/PlistBuddy",
    LSREGISTER,
)
GUARD = "path=(${path:#(/opt/homebrew|/usr/local)/bin})\n" + "".join(
    f'{tool}() {{ print -ru2 -- "{tool}: no stand-in in this test"; return 127; }}\n' for tool in GUARDED
)
# A stand-in fails like a program, at the call site: errexit inside a function would end the
# script without its EXIT trap (install.sh's rollback).
AS_PROGRAMS = 'for _f in ${(k)functions}; do functions[$_f]="setopt localoptions noerrexit; ${functions[$_f]}"; done; unset _f\n'


def copy_script(source: Path, target: Path) -> Path:
    """A copy of `source` at `target` that looks for the PROBED locations under $MAC_ROOT."""
    text = source.read_text(encoding="utf-8")
    for location in PROBED:
        assert location in text, f"{source.name} no longer looks at {location}: update PROBED"
        text = text.replace(location, "${MAC_ROOT}" + location)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    shutil.copymode(source, target)
    return target


def mac_env(
    zdotdir: Path, standins: str, *, root: Path, home: Path, path: str = SYSTEM_PATH, **extra: str
) -> dict[str, str]:
    """The environment for a zsh program with stand-ins: $ZDOTDIR/.zshenv defines GUARD and then
    `standins` (zsh functions, made AS_PROGRAMS); $MAC_ROOT is the fake root of the PROBED locations."""
    assert not {"/opt/homebrew/bin", "/usr/local/bin"} & set(path.split(":")), (
        "the real Homebrew must not be on PATH"
    )
    zdotdir.mkdir(parents=True, exist_ok=True)
    (zdotdir / ".zshenv").write_text(GUARD + standins + AS_PROGRAMS, encoding="utf-8")
    return {
        "HOME": str(home),
        "PATH": path,
        "ZDOTDIR": str(zdotdir),
        "MAC_ROOT": str(root),
        "LANG": "C.UTF-8",
        **extra,
    }


# ---------------------------------------------------------------------------------------
# run_install's stand-ins.
ZSHENV = r"""
_record() { print -r -- "${(pj:\x1f:)@}" >> "$INSTALLER_LOG"; }
uname() { print Darwin; }
brew() {
  _record brew "$@"
  case "$1" in
    list) [[ "$2" == --versions && " $INSTALLED_FORMULAS " == *" $3 "* ]] ;;
    install) true ;;
    --prefix) print -r -- "$FAKE_PREFIX/$2" ;;
  esac
}
python3.12() {
  _record python3.12 "$@"
  case "$1" in
    */install_checks.py) return ${CHECKS_STATUS:-0} ;;
    -m) if [[ "$2" == venv ]]; then
          env -u PYTHONPATH "$HOST_PYTHON" -m venv --without-pip "$3" || return
          /bin/mv "$3/bin/python" "$3/bin/python-real"
          cp "$VENV_PYTHON_STUB" "$3/bin/python"; cp "$VENV_CLI_STUB" "$3/bin/pudge"; cp "$VENV_CLI_STUB" "$3/bin/pudge-agent"
          chmod 755 "$3/bin/python" "$3/bin/pudge" "$3/bin/pudge-agent"
          return
        fi ;;
  esac
  env -u PYTHONPATH -u PYTHONHOME PATH="$STUB_BIN:$PATH" "$HOST_PYTHON" "$@"
}
/usr/bin/clang() {
  _record clang "$@"
  local out="${@[${@[(i)-o]}+1]}"
  print '#!/bin/sh' > "$out"
}
sips() { _record sips "$@"; : > "${@[-1]}"; }
iconutil() { _record iconutil "$@"; : > "${@[-1]}"; }
/usr/libexec/PlistBuddy() { _record PlistBuddy "$@"; }
LSREGISTER_PATH() { _record lsregister "$@"; }
launchctl() { _record launchctl "$@"; }
codesign() { _record codesign "$@"; }
killall() { _record killall "$@"; }
pkill() { _record pkill "$@"; }
/usr/bin/ditto() { _record ditto "$@"; cp -R "$1" "$2"; }
git() {
  _record git "$@"
  case "${@[3]}" in
    rev-parse) [[ "${@[4]}" == --short=12 ]] && print -r -- "${FAKE_REVISION[1,12]}" || print -r -- "$FAKE_REVISION" ;;
  esac
}
/usr/bin/git() { git "$@"; }
""".replace("LSREGISTER_PATH", LSREGISTER)

# The venv's bin/python: records the call, emulates pip, runs everything else for real.
VENV_PYTHON = r"""#!ZSH_PATH
rec=(venv-python "$@"); print -r -- "${(pj:\x1f:)rec}" >> "$INSTALLER_LOG"
real="${0:h}/python-real"
unset PYTHONHOME
export PATH="$STUB_BIN:$PATH"
if [[ "$1" == -m && "$2" == pip ]]; then
  shift 2
  exec "$real" -I "$FAKE_PIP" "$@"
fi
[[ "$1" == */mac_app_aliases.py ]] && exit 0
PYTHONPATH="$FAKE_MODULES" exec "$real" "$@"
"""

# bin/pudge and bin/pudge-agent of the venv (the CLI links point at them).
VENV_CLI = r"""#!ZSH_PATH
rec=(cli "$@"); print -r -- "${(pj:\x1f:)rec}" >> "$INSTALLER_LOG"
if [[ "$1" == --init-config ]]; then
  exec "${0:A:h}/python-real" -I -c 'from pudge.config import write_default_config; print(write_default_config(None))'
fi
"""

FAKE_PIP = r"""
import os, sys, sysconfig, tomllib, zipfile
from pathlib import Path

args = sys.argv[1:]
site = Path(sysconfig.get_paths()["purelib"])
if args[0] == "wheel":
    project = Path(args[1])
    version = tomllib.loads((project / "pyproject.toml").read_text())["project"]["version"]
    out = Path(args[args.index("-w") + 1]) / f"pudge-{version}-py3-none-any.whl"
    with zipfile.ZipFile(out, "w") as wheel:
        for path in sorted((project / "pudge").rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                wheel.write(path, path.relative_to(project).as_posix())
        wheel.writestr(f"pudge-{version}.dist-info/METADATA", f"Metadata-Version: 2.1\nName: pudge\nVersion: {version}\n")
elif args[0] == "install":
    target = args[-1]
    if target.endswith(".whl") and "--no-deps" in args:
        zipfile.ZipFile(target).extractall(site)
    elif target.startswith("manga-ocr") and os.environ.get("MANGA_OCR_INSTALLS", "1") == "1":
        (site / "manga_ocr").mkdir(parents=True, exist_ok=True)
        (site / "manga_ocr" / "__init__.py").write_text("")
"""

FAKE_APPKIT = r'''
"""Stand-in for PyObjC's AppKit: the running apps come from RUNNING_APPS."""
import json, os, time

_SPECS = json.loads(os.environ.get("RUNNING_APPS") or "[]")
_now = [time.monotonic()]


def _tick():
    _now[0] += 1.0
    return _now[0]


time.monotonic = _tick           # the installer's quit deadline passes without waiting
time.sleep = lambda _seconds: None


def _record(*fields):
    with open(os.environ["INSTALLER_LOG"], "a", encoding="utf-8") as log:
        log.write("\x1f".join(fields) + "\n")


class _App:
    def __init__(self, spec):
        self.spec, self.asked = spec, False

    def terminate(self):
        self.asked = True
        _record("terminate", self.spec["bundle_id"])
        return True

    def isTerminated(self):
        return self.asked and self.spec.get("quits", True)


class NSRunningApplication:
    @staticmethod
    def runningApplicationsWithBundleIdentifier_(bundle_id):
        return [_App(spec) for spec in _SPECS if spec["bundle_id"] == bundle_id]
'''

SITECUSTOMIZE = r"""
import os, sysconfig

_config_var = sysconfig.get_config_var


def get_config_var(name):
    # python3.X-config of the stand-in toolchain (the installer embeds Python with it).
    if name == "BINDIR" and os.environ.get("FAKE_PYTHON_BINDIR"):
        return os.environ["FAKE_PYTHON_BINDIR"]
    return _config_var(name)


sysconfig.get_config_var = get_config_var
"""

PYTHON_CONFIG = r"""#!ZSH_PATH
rec=(python-config "$@"); print -r -- "${(pj:\x1f:)rec}" >> "$INSTALLER_LOG"
case "$2" in
  --cflags) print -r -- "-I/fake/python/include" ;;
  --ldflags) print -r -- "-L/fake/python/lib -lpython-embed" ;;
esac
"""


@dataclass
class Install:
    completed: subprocess.CompletedProcess[str]
    calls: list[list[str]]
    home: Path
    project: Path

    @property
    def returncode(self) -> int:
        return self.completed.returncode

    @property
    def output(self) -> str:
        return self.completed.stdout + self.completed.stderr

    def calls_of(self, name: str) -> list[list[str]]:
        """Arguments of every call of one stand-in, in order."""
        return [call[1:] for call in self.calls if call[0] == name]

    def names(self) -> list[str]:
        return [call[0] for call in self.calls]

    @property
    def data_dir(self) -> Path:
        return self.home / ".local" / "share" / "pudge"

    @property
    def venv(self) -> Path:
        return self.data_dir / "venv"

    @property
    def app(self) -> Path:
        return self.home / "Applications" / "pudge.app"

    @property
    def config(self) -> Path:
        return self.home / ".config" / "pudge" / "config.toml"

    @property
    def agent_plist(self) -> Path:
        return self.home / "Library" / "LaunchAgents" / "com.pudge.agent.plist"

    @property
    def launcher_source(self) -> Path:
        return self.data_dir / "app-build" / "pudge-launcher.m"

    def site_packages(self) -> Path:
        return next((self.venv / "lib").glob("python*/site-packages"))

    def plist(self, path: Path) -> dict[str, Any]:
        return plistlib.loads(path.read_bytes())

    def prefix(self, tool: str) -> str:
        return str(self.home.parent / "prefix" / tool)


def _executable(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.replace("ZSH_PATH", str(ZSH)), encoding="utf-8")
    path.chmod(0o755)
    return path


def make_project(
    work: Path,
    *,
    version: str = "9.8.7",
    release_wheel: bool = True,
    development: bool = False,
    brand: dict[str, str] | None = None,
) -> Path:
    """A release folder (install.sh, pudge/brand.env, a wheel) or, with `development`, a checkout."""
    project = work / "project"
    (project / "pudge" / "assets").mkdir(parents=True, exist_ok=True)
    copy_script(ROOT / "install.sh", project / "install.sh")
    for name in ("brand.env", "legacy_install.py", "mac_app_aliases.py", "install_checks.py"):
        shutil.copy2(ROOT / "pudge" / name, project / "pudge" / name)
    if brand:
        text = (project / "pudge" / "brand.env").read_text(encoding="utf-8")
        for key, value in brand.items():
            text = (
                "\n".join(
                    f'{key}="{value}"' if line.startswith(f"{key}=") else line for line in text.splitlines()
                )
                + "\n"
            )
        (project / "pudge" / "brand.env").write_text(text, encoding="utf-8")
    (project / "pudge" / "assets" / "app-icon.png").write_bytes(b"\x89PNG")
    set_version(project, version)
    if development:
        (project / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
        (project / ".github" / "release").mkdir(parents=True)
        shutil.copy2(ROOT / ".github" / "release" / "write_build_info.py", project / ".github" / "release")
    elif release_wheel:
        build_wheel(project, version)
    return project


def set_version(project: Path, version: str) -> None:
    # The installed package: this version, with the repository's modules behind it.
    (project / "pudge" / "__init__.py").write_text(
        f"__version__ = {version!r}\n__path__.append({str(ROOT / 'pudge')!r})\n", encoding="utf-8"
    )
    (project / "pyproject.toml").write_text(
        f'[project]\nname = "pudge"\nversion = "{version}"\n', encoding="utf-8"
    )


def build_wheel(project: Path, version: str, extra: dict[str, str] | None = None) -> Path:
    """The release wheel next to install.sh (as in a release ZIP)."""
    for old in project.glob("pudge-*.whl"):
        old.unlink()
    wheel = project / f"pudge-{version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.write(project / "pudge" / "__init__.py", "pudge/__init__.py")
        for name, text in (extra or {}).items():
            archive.writestr(name, text)
        archive.writestr(
            f"pudge-{version}.dist-info/METADATA", f"Metadata-Version: 2.1\nName: pudge\nVersion: {version}\n"
        )
    return wheel


def run_install(
    tmp_path: Path,
    *,
    project: Path | None = None,
    update: bool = False,
    installed: tuple[str, ...] = FORMULAS,
    tools: tuple[str, ...] = TOOLS,
    running_apps: list[dict[str, Any]] | None = None,
    checks_status: int = 0,
    env: dict[str, str] | None = None,
    args: tuple[str, ...] = (),
) -> Install:
    """Runs install.sh (`--update` with `update`) for the home folder in tmp_path; a second call
    with the same tmp_path updates that installation."""
    if ZSH is None:
        pytest.skip("zsh is not installed")
    work = tmp_path
    project = project or (work / "project" if (work / "project").is_dir() else make_project(work))
    home = work / "home"
    home.mkdir(exist_ok=True)
    prefix = work / "prefix"
    shutil.rmtree(prefix, ignore_errors=True)
    for tool in tools:
        _executable(prefix / tool, "#!/bin/sh\n")
    stubs = work / "stubs"
    if not stubs.is_dir():
        _executable(stubs / "venv-python", VENV_PYTHON)
        _executable(stubs / "venv-cli", VENV_CLI)
        _executable(stubs / "bindir" / f"python{PY_VERSION}-config", PYTHON_CONFIG)
        _executable(stubs / "bin" / "security", "#!/bin/sh\nexit 44\n")  # no Keychain
        (stubs / "modules").mkdir()
        (stubs / "modules" / "AppKit.py").write_text(FAKE_APPKIT, encoding="utf-8")
        (stubs / "modules" / "sitecustomize.py").write_text(SITECUSTOMIZE, encoding="utf-8")
        (stubs / "fake_pip.py").write_text(FAKE_PIP, encoding="utf-8")
        (work / "tmp").mkdir(exist_ok=True)
        (work / "cwd").mkdir(exist_ok=True)
    log = work / "installer.log"
    log.write_text("", encoding="utf-8")
    # No Homebrew at the standard locations ($MAC_ROOT is empty): `brew` is the stand-in function.
    environment = mac_env(
        stubs / "zdot",
        ZSHENV,
        root=work / "root",
        home=home,
        TMPDIR=str(work / "tmp"),
        INSTALLER_LOG=str(log),
        FAKE_PREFIX=str(prefix),
        INSTALLED_FORMULAS=" ".join(installed),
        HOST_PYTHON=sys.executable,
        STUB_BIN=str(stubs / "bin"),
        VENV_PYTHON_STUB=str(stubs / "venv-python"),
        VENV_CLI_STUB=str(stubs / "venv-cli"),
        FAKE_MODULES=str(stubs / "modules"),
        FAKE_PIP=str(stubs / "fake_pip.py"),
        FAKE_PYTHON_BINDIR=str(stubs / "bindir"),
        FAKE_REVISION=FAKE_REVISION,
        RUNNING_APPS=json.dumps(running_apps or []),
        CHECKS_STATUS=str(checks_status),
    )
    environment.update(env or {})
    command = [ZSH, str(project / "install.sh"), *(("--update",) if update else ()), *args]
    completed = subprocess.run(
        command, capture_output=True, text=True, env=environment, timeout=120, cwd=work / "cwd"
    )
    calls = [line.split(SEP) for line in log.read_text(encoding="utf-8").splitlines() if line]
    return Install(completed, calls, home, project)


# Stand-ins for Apple's frameworks on the GNU Objective-C runtime (gcc's libobjc), enough for
# the launcher install.sh writes: they record what the launcher does in FAKE_TRACE.
FAKE_FRAMEWORK_HEADERS = {
    "Foundation/Foundation.h": r"""/* Stand-in for Apple's frameworks: what Pudge's launcher uses, on the GNU Objective-C runtime. */
#pragma clang diagnostic ignored "-Wobjc-root-class"
#include <stddef.h>
#include <stdint.h>
typedef signed char BOOL;
#define YES ((BOOL)1)
#define NO ((BOOL)0)
#define nil ((id)0)
typedef long NSInteger;
typedef unsigned long NSUInteger;
typedef double NSTimeInterval;
@interface NSObject { Class isa; }
+ (id)alloc; + (id)new; - (id)init; - (void)release; - (id)retain; - (id)autorelease;
@end
@interface NSString : NSObject
+ (id)stringWithUTF8String:(const char *)value;
- (const char *)UTF8String;
- (const char *)fileSystemRepresentation;
@end
@interface FakeConstantString : NSString { char *c_string; unsigned int len; }
@end
@interface NSAutoreleasePool : NSObject
- (void)drain;
@end
@interface NSError : NSObject
@end
@interface NSDate : NSObject
+ (id)dateWithTimeIntervalSinceNow:(NSTimeInterval)seconds;
@end
@interface NSRunLoop : NSObject
+ (NSRunLoop *)currentRunLoop;
- (void)runUntilDate:(NSDate *)date;
@end
@interface NSUUID : NSObject
+ (NSUUID *)UUID;
- (NSString *)UUIDString;
@end
@interface NSBundle : NSObject
+ (NSBundle *)mainBundle;
- (NSString *)pathForResource:(NSString *)name ofType:(NSString *)type;
@end
typedef struct fake_semaphore *dispatch_semaphore_t;
typedef uint64_t dispatch_time_t;
#define DISPATCH_TIME_NOW ((dispatch_time_t)0)
#define NSEC_PER_SEC 1000000000ull
dispatch_semaphore_t dispatch_semaphore_create(long value);
long dispatch_semaphore_signal(dispatch_semaphore_t semaphore);
long dispatch_semaphore_wait(dispatch_semaphore_t semaphore, dispatch_time_t timeout);
dispatch_time_t dispatch_time(dispatch_time_t when, int64_t delta);
void fake_trace(const char *format, ...);
""",
    "AppKit/AppKit.h": r"""#import <Foundation/Foundation.h>
typedef NSInteger NSApplicationActivationPolicy;
#define NSApplicationActivationPolicyProhibited ((NSApplicationActivationPolicy)2)
@interface NSApplication : NSObject
@property (class, readonly) NSApplication *sharedApplication;
- (BOOL)setActivationPolicy:(NSApplicationActivationPolicy)policy;
@end
""",
    "UserNotifications/UserNotifications.h": r"""#import <Foundation/Foundation.h>
typedef NSUInteger UNNotificationPresentationOptions;
#define UNNotificationPresentationOptionSound ((UNNotificationPresentationOptions)2)
#define UNNotificationPresentationOptionList ((UNNotificationPresentationOptions)8)
#define UNNotificationPresentationOptionBanner ((UNNotificationPresentationOptions)16)
typedef NSInteger UNAuthorizationStatus;
#define UNAuthorizationStatusDenied ((UNAuthorizationStatus)1)
#define UNAuthorizationStatusAuthorized ((UNAuthorizationStatus)2)
#define UNAuthorizationStatusProvisional ((UNAuthorizationStatus)3)
@class UNUserNotificationCenter, UNNotification;
@protocol UNUserNotificationCenterDelegate
@end
@interface UNNotificationSettings : NSObject
@property (readonly) UNAuthorizationStatus authorizationStatus;
@end
@interface UNNotificationSound : NSObject
+ (UNNotificationSound *)defaultSound;
@end
@interface UNMutableNotificationContent : NSObject { NSString *title, *subtitle, *body; UNNotificationSound *sound; }
@property (assign) NSString *title;
@property (assign) NSString *subtitle;
@property (assign) NSString *body;
@property (assign) UNNotificationSound *sound;
@end
@interface UNNotification : NSObject
@end
@interface UNNotificationRequest : NSObject { @public UNMutableNotificationContent *content; }
+ (UNNotificationRequest *)requestWithIdentifier:(NSString *)identifier content:(UNMutableNotificationContent *)content trigger:(id)trigger;
@end
@interface UNUserNotificationCenter : NSObject { id delegate; }
+ (UNUserNotificationCenter *)currentNotificationCenter;
@property (assign) id<UNUserNotificationCenterDelegate> delegate;
- (void)getNotificationSettingsWithCompletionHandler:(void (^)(UNNotificationSettings *settings))handler;
- (void)addNotificationRequest:(UNNotificationRequest *)request withCompletionHandler:(void (^)(NSError *error))handler;
@end
""",
}
FAKE_FRAMEWORKS = r"""#import <AppKit/AppKit.h>
#import <UserNotifications/UserNotifications.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <Python.h>
id class_createInstance(Class class_, size_t extra);

/* Blocks without a blocks runtime: the stand-ins call them synchronously. */
void *_NSConcreteStackBlock[32];
void *_NSConcreteGlobalBlock[32];
void _Block_object_assign(void *destination, const void *object, const int flags) { *(const void **)destination = object; }
void _Block_object_dispose(const void *object, const int flags) {}

void fake_trace(const char *format, ...) {
    const char *path = getenv("FAKE_TRACE");
    FILE *file = path ? fopen(path, "a") : NULL;
    if (!file) return;
    va_list args; va_start(args, format); vfprintf(file, format, args); va_end(args);
    fputc('\n', file); fclose(file);
}

struct fake_semaphore { long value; };
dispatch_semaphore_t dispatch_semaphore_create(long value) { dispatch_semaphore_t s = malloc(sizeof *s); s->value = value; return s; }
long dispatch_semaphore_signal(dispatch_semaphore_t s) { s->value++; return 0; }
long dispatch_semaphore_wait(dispatch_semaphore_t s, dispatch_time_t timeout) { if (s->value > 0) { s->value--; return 0; } return 1; }
dispatch_time_t dispatch_time(dispatch_time_t when, int64_t delta) { return when + (dispatch_time_t)delta; }

@implementation NSObject
+ (id)alloc { return class_createInstance(self, 0); }
+ (id)new { return [[self alloc] init]; }
- (id)init { return self; }
- (void)release {}
- (id)retain { return self; }
- (id)autorelease { return self; }
@end

@interface FakeString : NSString { char *value; }
@end
@implementation FakeString
- (id)initWithUTF8:(const char *)text { value = strdup(text); return self; }
- (const char *)UTF8String { return value; }
@end
@implementation NSString
+ (id)stringWithUTF8String:(const char *)value { return [[FakeString alloc] initWithUTF8:value]; }
- (const char *)UTF8String { return ""; }
- (const char *)fileSystemRepresentation { return [self UTF8String]; }
@end
@implementation FakeConstantString
- (const char *)UTF8String { return c_string; }
@end

@implementation NSAutoreleasePool
- (id)init { fake_trace("pool-open python=%d", Py_IsInitialized()); return self; }
- (void)drain { fake_trace("pool-drain python=%d", Py_IsInitialized()); }
- (void)release { [self drain]; }
@end

@implementation NSError
@end
@implementation NSDate
+ (id)dateWithTimeIntervalSinceNow:(NSTimeInterval)seconds { return [self new]; }
@end
@implementation NSRunLoop
+ (NSRunLoop *)currentRunLoop { return [self new]; }
- (void)runUntilDate:(NSDate *)date {}
@end
@implementation NSUUID
+ (NSUUID *)UUID { return [self new]; }
- (NSString *)UUIDString { return [NSString stringWithUTF8String:"uuid"]; }
@end
@implementation NSBundle
+ (NSBundle *)mainBundle { return [self new]; }
- (NSString *)pathForResource:(NSString *)name ofType:(NSString *)type {
    const char *folder = getenv("FAKE_BUNDLE_RESOURCES");
    if (!folder) return nil;
    char path[4096];
    snprintf(path, sizeof path, "%s/%s.%s", folder, [name UTF8String], [type UTF8String]);
    FILE *file = fopen(path, "r");
    if (!file) return nil;
    fclose(file);
    return [NSString stringWithUTF8String:path];
}
@end

@implementation NSApplication
+ (NSApplication *)sharedApplication { return [self new]; }
- (BOOL)setActivationPolicy:(NSApplicationActivationPolicy)policy { fake_trace("activation-policy %ld", (long)policy); return YES; }
@end

@implementation UNNotificationSettings
- (UNAuthorizationStatus)authorizationStatus { const char *s = getenv("FAKE_NOTIFICATION_STATUS"); return s ? atol(s) : UNAuthorizationStatusAuthorized; }
@end
@implementation UNNotificationSound
+ (UNNotificationSound *)defaultSound { return [self new]; }
@end
@implementation UNMutableNotificationContent
@synthesize title, subtitle, body, sound;
@end
@implementation UNNotification
@end
@implementation UNNotificationRequest
+ (UNNotificationRequest *)requestWithIdentifier:(NSString *)identifier content:(UNMutableNotificationContent *)content trigger:(id)trigger {
    UNNotificationRequest *request = [self new]; request->content = content; return request;
}
@end
@implementation UNUserNotificationCenter
@synthesize delegate;
+ (UNUserNotificationCenter *)currentNotificationCenter { return [self new]; }
- (void)getNotificationSettingsWithCompletionHandler:(void (^)(UNNotificationSettings *settings))handler { handler([UNNotificationSettings new]); }
- (void)addNotificationRequest:(UNNotificationRequest *)request withCompletionHandler:(void (^)(NSError *error))handler {
    UNMutableNotificationContent *content = request->content;
    fake_trace("notification title=%s subtitle=%s body=%s sound=%d delegate=%d", [content.title UTF8String], [content.subtitle UTF8String],
               [content.body UTF8String], content.sound != nil, delegate != nil);
    handler(nil);
}
@end
"""


def build_launcher(source: Path, folder: Path) -> Path:
    """Compiles the launcher source install.sh wrote against the stand-in frameworks and this
    Python (Linux with clang and gcc's Objective-C runtime; skips elsewhere)."""
    import ctypes.util
    import sysconfig

    clang = shutil.which("clang")
    objc = ctypes.util.find_library("objc")
    include = Path(sysconfig.get_paths()["include"])
    libdir = sysconfig.get_config_var("LIBDIR")
    if not sys.platform.startswith("linux") or not clang or not objc or not (include / "Python.h").is_file():
        pytest.skip("building the launcher off a Mac needs clang, gcc's libobjc and Python.h")
    for name, text in FAKE_FRAMEWORK_HEADERS.items():
        (folder / "include" / name).parent.mkdir(parents=True, exist_ok=True)
        (folder / "include" / name).write_text(text, encoding="utf-8")
    (folder / "frameworks.m").write_text(FAKE_FRAMEWORKS, encoding="utf-8")
    launcher = folder / "pudge-launcher"
    built = subprocess.run(
        [
            clang,
            "-x",
            "objective-c",
            "-fobjc-runtime=gcc",
            "-fblocks",
            "-fconstant-string-class=FakeConstantString",
            "-w",
            f"-I{folder / 'include'}",
            f"-I{include}",
            str(source),
            str(folder / "frameworks.m"),
            "-o",
            str(launcher),
            f"-L{libdir}",
            f"-Wl,-rpath,{libdir}",
            f"-lpython{sysconfig.get_config_var('LDVERSION')}",
            f"-l:{objc}",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert built.returncode == 0, built.stderr
    return launcher


def clone_install(source: Path, target: Path) -> Path:
    """A copy of an installation made by run_install (to update it without touching the original)."""
    shutil.copytree(source, target, symlinks=True)
    return target
