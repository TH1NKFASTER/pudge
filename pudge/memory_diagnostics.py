"""What the window process keeps in memory: RSS, the sizes of its caches, an hourly log line.

``cache_sizes`` walks the module-level caches of ``pudge.*`` (``lru_cache`` wrappers, dicts,
OrderedDict memories, lists, sets, deques, numpy arrays) and the containers held by the running
services (the api object and the services it owns), so a report names what grew without each
cache registering itself. ``MemoryReporter`` logs ``EVENT memory rss_mb=… caches=…`` every hour
and on demand (the runtime debug bundle); every 10 minutes it lets services drop what they keep
only for speed and have not used for a while (``release_idle_memory``).
"""

from __future__ import annotations

import contextlib
import ctypes
import gc
import itertools
import os
import sys
import threading
import time
import weakref
from collections import deque
from typing import Any

_CONTAINERS = (dict, list, set, frozenset, deque, tuple)
# Services reached from the api object whose own containers are reported (api.manager.db…).
_SERVICE_DEPTH = 3
# A container larger than this many entries is sized from its first ones; sizing one cache
# never visits more than _DEEP_BUDGET objects.
_SAMPLE_ENTRIES = 2_000
_DEEP_BUDGET = 400_000
_DEEP_TOTAL_BUDGET = 800_000  # one cache_sizes() walk, all caches together


# Where the last rss_mb() came from ("proc", "task_info", "proc_pid_rusage", "ps", "none").
RSS_SOURCE = "none"


def rss_mb() -> float:
    """Current resident memory of this process in MB (not the peak), 0.0 when unknown."""
    global RSS_SOURCE
    if sys.platform.startswith("linux"):
        try:
            with open("/proc/self/statm", encoding="ascii") as handle:
                pages = int(handle.read().split()[1])
            RSS_SOURCE = "proc"
            return pages * os.sysconf("SC_PAGE_SIZE") / 2**20
        except (OSError, ValueError, IndexError):
            pass
    elif sys.platform == "darwin":
        for source, reader in (
            ("task_info", _darwin_task_info_rss),
            ("proc_pid_rusage", _darwin_rusage_rss),
        ):
            try:
                value = int(reader())
            except Exception:  # noqa: BLE001 - try the next source
                continue
            if value > 0:
                RSS_SOURCE = source
                return value / 2**20
    try:  # last resort, any Unix: one short ps call
        import subprocess

        output = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(os.getpid())], capture_output=True, text=True, timeout=5
        ).stdout
        kilobytes = int(output.strip().split()[0])
        RSS_SOURCE = "ps"
        return kilobytes / 1024
    except Exception:  # noqa: BLE001 - diagnostics only
        RSS_SOURCE = "none"
        return 0.0


def _libsystem() -> Any:
    return ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)


_TASK_INFO: Any = None  # (function, task port, struct class) once set up


def _darwin_task_info_rss() -> int:
    """task_info(mach_task_self(), MACH_TASK_BASIC_INFO) -> resident_size (set up once)."""
    global _TASK_INFO
    if _TASK_INFO is None:

        class _TimeValue(ctypes.Structure):
            _fields_ = [("seconds", ctypes.c_int32), ("microseconds", ctypes.c_int32)]

        class _BasicInfo(ctypes.Structure):  # struct mach_task_basic_info (48 bytes)
            _pack_ = 4
            _fields_ = [
                ("virtual_size", ctypes.c_uint64),
                ("resident_size", ctypes.c_uint64),
                ("resident_size_max", ctypes.c_uint64),
                ("user_time", _TimeValue),
                ("system_time", _TimeValue),
                ("policy", ctypes.c_int32),
                ("suspend_count", ctypes.c_int32),
            ]

        libc = _libsystem()
        # mach_task_self() is a macro over the global mach_task_self_ (not always a function).
        task = ctypes.c_uint32.in_dll(libc, "mach_task_self_").value
        function = libc.task_info
        function.argtypes = [
            ctypes.c_uint32,
            ctypes.c_int,
            ctypes.POINTER(_BasicInfo),
            ctypes.POINTER(ctypes.c_uint32),
        ]
        function.restype = ctypes.c_int
        _TASK_INFO = (function, task, _BasicInfo)
    function, task, info_type = _TASK_INFO
    info = info_type()
    count = ctypes.c_uint32(ctypes.sizeof(info_type) // 4)  # MACH_TASK_BASIC_INFO_COUNT
    result = function(task, 20, ctypes.byref(info), ctypes.byref(count))  # MACH_TASK_BASIC_INFO
    if result != 0:
        raise OSError(result, "task_info")
    return int(info.resident_size)


def _darwin_rusage_rss() -> int:
    """proc_pid_rusage(getpid(), RUSAGE_INFO_V0) -> ri_resident_size (offset 64)."""
    libc = _libsystem()
    buffer = (ctypes.c_uint64 * 64)()  # rusage_info_v0 is 96 bytes; room for any version
    libc.proc_pid_rusage.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
    libc.proc_pid_rusage.restype = ctypes.c_int
    if libc.proc_pid_rusage(os.getpid(), 0, ctypes.byref(buffer)) != 0:
        raise OSError(ctypes.get_errno(), "proc_pid_rusage")
    # uuid (16 bytes) + user, system, pkg_idle_wkups, interrupt_wkups, pageins, wired_size
    return int(buffer[2 + 6])


# ------------------------------------------------------------------ where RSS lives (MEM4)
#
# RSS on macOS counts file-backed pages (dylibs, the shared cache, mapped files) and Python's
# own arenas (anonymous mmap, not a malloc zone), and it drops when the system compresses idle
# pages; Activity Monitor's "Memory" is phys_footprint. These say which of them RSS is.

_VM_FIELDS = (
    "virtual_size",
    "region_count_page_size",  # two 32-bit fields read as one
    "resident_size",
    "resident_size_peak",
    "device",
    "device_peak",
    "internal",
    "internal_peak",
    "external",
    "external_peak",
    "reusable",
    "reusable_peak",
    "purgeable_volatile_pmap",
    "purgeable_volatile_resident",
    "purgeable_volatile_virtual",
    "compressed",
    "compressed_peak",
    "compressed_lifetime",
    "phys_footprint",
    "min_address",
    "max_address",
)


# rev3..rev7 of struct task_vm_info (xnu osfmk/mach/task_info.h, #pragma pack(4)): the
# kernel's ledgers. phys_footprint also counts what internal/compressed do not (graphics and
# media memory tagged by IOKit/Metal, network buffers, page tables, IOKit mappings).
_LEDGER_FIELDS = (
    ("ledger_phys_footprint_peak", ctypes.c_int64),
    ("ledger_purgeable_nonvolatile", ctypes.c_int64),
    ("ledger_purgeable_novolatile_compressed", ctypes.c_int64),
    ("ledger_purgeable_volatile", ctypes.c_int64),
    ("ledger_purgeable_volatile_compressed", ctypes.c_int64),
    ("ledger_tag_network_nonvolatile", ctypes.c_int64),
    ("ledger_tag_network_nonvolatile_compressed", ctypes.c_int64),
    ("ledger_tag_network_volatile", ctypes.c_int64),
    ("ledger_tag_network_volatile_compressed", ctypes.c_int64),
    ("ledger_tag_media_footprint", ctypes.c_int64),
    ("ledger_tag_media_footprint_compressed", ctypes.c_int64),
    ("ledger_tag_media_nofootprint", ctypes.c_int64),
    ("ledger_tag_media_nofootprint_compressed", ctypes.c_int64),
    ("ledger_tag_graphics_footprint", ctypes.c_int64),
    ("ledger_tag_graphics_footprint_compressed", ctypes.c_int64),
    ("ledger_tag_graphics_nofootprint", ctypes.c_int64),
    ("ledger_tag_graphics_nofootprint_compressed", ctypes.c_int64),
    ("ledger_tag_neural_footprint", ctypes.c_int64),
    ("ledger_tag_neural_footprint_compressed", ctypes.c_int64),
    ("ledger_tag_neural_nofootprint", ctypes.c_int64),
    ("ledger_tag_neural_nofootprint_compressed", ctypes.c_int64),
    ("limit_bytes_remaining", ctypes.c_uint64),  # rev4
    ("decompressions", ctypes.c_int32),  # rev5
    ("ledger_swapins", ctypes.c_int64),  # rev6
    ("ledger_tag_neural_nofootprint_total", ctypes.c_int64),  # rev7
    ("ledger_tag_neural_nofootprint_peak", ctypes.c_int64),
)


class _TaskVmInfo(ctypes.Structure):  # struct task_vm_info up to rev7
    _pack_ = 4
    _fields_ = [(name, ctypes.c_uint64) for name in _VM_FIELDS] + list(_LEDGER_FIELDS)


# Ledger -> short name in the vm= part (bytes, shown in MB when the kernel filled it and != 0).
LEDGER_NAMES = (
    ("ledger_tag_graphics_footprint", "graphics"),
    ("ledger_tag_graphics_footprint_compressed", "graphics_c"),
    ("ledger_tag_graphics_nofootprint", "graphics_nofp"),
    ("ledger_tag_media_footprint", "media"),
    ("ledger_tag_media_footprint_compressed", "media_c"),
    ("ledger_tag_media_nofootprint", "media_nofp"),
    ("ledger_tag_neural_footprint", "neural"),
    ("ledger_tag_neural_nofootprint", "neural_nofp"),
    ("ledger_tag_network_nonvolatile", "network"),
    ("ledger_tag_network_volatile", "network_vol"),
    ("ledger_purgeable_nonvolatile", "purgeable_nonvol"),
    ("ledger_purgeable_volatile", "purgeable_vol"),
    ("ledger_phys_footprint_peak", "footprint_peak"),
    ("limit_bytes_remaining", "limit_left"),
    ("ledger_swapins", "swapins"),
)


TASK_VM_INFO = 22
_TASK_VM: Any = None  # (function, task) once set up


_TASK_VM_WORDS = [0]  # 32-bit words the kernel filled in the last call


def _filled(name: str) -> bool:
    """The kernel's struct reached field ``name`` (older kernels return a shorter one)."""
    field = getattr(_TaskVmInfo, name)
    return _TASK_VM_WORDS[0] * 4 >= field.offset + field.size


def _darwin_task_vm_info() -> _TaskVmInfo:
    global _TASK_VM
    if _TASK_VM is None:
        libc = _libsystem()
        task = ctypes.c_uint32.in_dll(libc, "mach_task_self_").value
        function = libc.task_info
        function.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        function.restype = ctypes.c_int
        _TASK_VM = (function, task)
    function, task = _TASK_VM
    info = _TaskVmInfo()
    count = ctypes.c_uint32(ctypes.sizeof(_TaskVmInfo) // 4)
    result = function(task, TASK_VM_INFO, ctypes.byref(info), ctypes.byref(count))
    if result != 0:
        raise OSError(result, "task_info(TASK_VM_INFO)")
    _TASK_VM_WORDS[0] = min(int(count.value), ctypes.sizeof(_TaskVmInfo) // 4)
    if count.value * 4 < _TaskVmInfo.phys_footprint.offset + 8:
        info.phys_footprint = 0  # an old kernel without rev1: no footprint
    return info


def _linux_status() -> dict[str, float]:
    values: dict[str, float] = {}
    with open("/proc/self/status", encoding="ascii", errors="replace") as handle:
        for line in handle:
            key, _sep, rest = line.partition(":")
            parts = rest.split()
            if key in {"RssAnon", "RssFile", "RssShmem", "VmSwap", "VmRSS"} and parts:
                values[key] = int(parts[0]) / 1024
    return values


def vm_info() -> dict[str, float]:
    """MB: ``footprint`` (what Activity Monitor shows), ``internal`` (anonymous resident),
    ``external`` (file-backed resident), ``compressed``, ``reusable``, ``purgeable``. Linux:
    RssAnon / RssFile+RssShmem / VmSwap. Empty when unknown."""
    mb = 2**20
    try:
        if sys.platform == "darwin":
            info = _darwin_task_vm_info()
            result = {
                "footprint": info.phys_footprint / mb,
                "internal": info.internal / mb,
                "external": info.external / mb,
                "compressed": info.compressed / mb,
                "reusable": info.reusable / mb,
            }
            # Filled only for flavor TASK_VM_INFO_PURGEABLE (xnu zeroes it for TASK_VM_INFO):
            # shown when the kernel reports it, never as a misleading 0.
            if info.purgeable_volatile_resident:
                result["purgeable"] = info.purgeable_volatile_resident / mb
            if not info.phys_footprint:
                result.pop("footprint")
            else:
                # What the footprint has beyond anonymous resident + compressed memory.
                rest = (info.phys_footprint - info.internal - info.compressed) / mb
                if abs(rest) >= 0.05:
                    result["rest"] = rest
            for field, short in LEDGER_NAMES:
                if _filled(field):
                    value = getattr(info, field)
                    if value and (field != "limit_bytes_remaining" or value < 2**62):
                        result[short] = value / mb
        elif sys.platform.startswith("linux"):
            status = _linux_status()
            result = {
                "footprint": status.get("RssAnon", 0.0) + status.get("VmSwap", 0.0),
                "internal": status.get("RssAnon", 0.0),
                "external": status.get("RssFile", 0.0) + status.get("RssShmem", 0.0),
                "compressed": status.get("VmSwap", 0.0),
            }
        else:
            return {}
    except Exception:  # noqa: BLE001 - diagnostics only
        return {}
    return {key: round(float(value), 1) for key, value in result.items()}


def footprint_mb() -> float:
    """phys_footprint in MB (0.0 when unknown); one task_info call."""
    return float(vm_info().get("footprint", 0.0))


class _SubmapInfo64(ctypes.Structure):  # struct vm_region_submap_info_64 (19 natural_t)
    _pack_ = 4
    _fields_ = [
        ("protection", ctypes.c_int),
        ("max_protection", ctypes.c_int),
        ("inheritance", ctypes.c_uint),
        ("offset", ctypes.c_uint64),
        ("user_tag", ctypes.c_uint),
        ("pages_resident", ctypes.c_uint),
        ("pages_shared_now_private", ctypes.c_uint),
        ("pages_swapped_out", ctypes.c_uint),
        ("pages_dirtied", ctypes.c_uint),
        ("ref_count", ctypes.c_uint),
        ("shadow_depth", ctypes.c_ushort),
        ("external_pager", ctypes.c_ubyte),
        ("share_mode", ctypes.c_ubyte),
        ("is_submap", ctypes.c_int),
        ("behavior", ctypes.c_int),
        ("object_id", ctypes.c_uint),
        ("user_wired_count", ctypes.c_ushort),
        ("pages_reusable", ctypes.c_uint),
        ("object_id_full", ctypes.c_uint64),
    ]


# VM_MEMORY_* tags (mach/vm_statistics.h) worth a name; others print as tag<N>.
_TAG_GROUPS = {
    0: "untagged",  # anonymous mmap: Python's object arenas (obmalloc), ctypes, numpy's big buffers
    **{tag: "malloc" for tag in (1, 2, 3, 4, 6, 7, 8, 9, 11, 12)},
    30: "stack",
    33: "dylib",
    40: "appkit",
    41: "foundation",
    42: "coregraphics",
    51: "layerkit",
    52: "cgimage",
    **{tag: "coregraphics" for tag in (54, 55, 56, 57, 58)},
    60: "dyld",
    61: "dyld",
    62: "sqlite",
    **{tag: "javascriptcore" for tag in (63, 64, 65)},
    70: "imageio",
    88: "iosurface",
}
REGION_LIMIT = 60_000
REGION_SECONDS = 0.5


def group_regions(regions: Any, page_size: int) -> dict[str, tuple[float, float]]:
    """(user_tag, file_backed, pages_resident, pages_dirtied) per region -> group -> (resident
    MB, dirty MB). File-backed pages (dylibs, the shared cache, mapped files) are one group."""
    totals: dict[str, list[float]] = {}
    for tag, file_backed, resident, dirtied in regions:
        group = "file" if file_backed else _TAG_GROUPS.get(int(tag), f"tag{int(tag)}")
        entry = totals.setdefault(group, [0.0, 0.0])
        entry[0] += int(resident) * page_size
        entry[1] += int(dirtied) * page_size
    return {name: (round(res / 2**20, 1), round(dirty / 2**20, 1)) for name, (res, dirty) in totals.items()}


_REGION_RECURSE: Any = None


def _darwin_regions(limit: int = REGION_LIMIT, seconds: float = REGION_SECONDS, *, sizes: bool = False):
    """(user_tag, external_pager, pages_resident, pages_dirtied) of every leaf region
    (``sizes``: with the region's size in bytes as a fifth value)."""
    global _REGION_RECURSE
    if _REGION_RECURSE is None:
        libc = _libsystem()
        task = ctypes.c_uint32.in_dll(libc, "mach_task_self_").value
        function = libc.mach_vm_region_recurse
        function.argtypes = [
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        function.restype = ctypes.c_int
        _REGION_RECURSE = (function, task)
    function, task = _REGION_RECURSE
    address = ctypes.c_uint64(0)
    size = ctypes.c_uint64(0)
    depth = ctypes.c_uint32(0)
    info = _SubmapInfo64()
    deadline = time.monotonic() + seconds
    for _index in range(limit):
        count = ctypes.c_uint32(ctypes.sizeof(_SubmapInfo64) // 4)
        if function(
            task,
            ctypes.byref(address),
            ctypes.byref(size),
            ctypes.byref(depth),
            ctypes.byref(info),
            ctypes.byref(count),
        ):
            return  # KERN_INVALID_ADDRESS: past the last region
        if info.is_submap:
            depth.value += 1
            continue
        if sizes:
            yield (
                int(info.user_tag),
                bool(info.external_pager),
                int(info.pages_resident),
                int(info.pages_dirtied),
                int(size.value),
            )
        else:
            yield (
                int(info.user_tag),
                bool(info.external_pager),
                int(info.pages_resident),
                int(info.pages_dirtied),
            )
        if not size.value or time.monotonic() > deadline:
            return
        address.value += size.value


def vm_regions(*, top: int = 8) -> dict[str, tuple[float, float]]:
    """macOS: resident / dirty MB by kind of memory (vmmap --summary in short). Empty elsewhere."""
    if sys.platform != "darwin":
        return {}
    try:
        import resource

        grouped = group_regions(_darwin_regions(), resource.getpagesize())
    except Exception:  # noqa: BLE001 - diagnostics only
        return {}
    ranked = sorted(grouped.items(), key=lambda item: -item[1][0])[:top]
    return dict(ranked)


IMAGE_TAGS = {52: "cgimage", 70: "imageio"}


def image_regions_summary(regions: Any, page_size: int, *, top: int = 4) -> str:
    """``cgimage:<regions>/<resident MB>[<MB>x<count>,…]`` per decoded-image kind: the sizes tell
    what was decoded (a 1024 px RGBA icon is a 4 MB region, a 2048 px one 16 MB). '' without any.
    ``regions``: (user_tag, file_backed, pages_resident, pages_dirtied, bytes)."""
    found: dict[str, list[tuple[float, float]]] = {}
    for tag, file_backed, resident, _dirtied, size in regions:
        name = IMAGE_TAGS.get(int(tag))
        if name is None or file_backed or not resident:
            continue
        found.setdefault(name, []).append((int(size) / 2**20, int(resident) * page_size / 2**20))
    parts = []
    for name in ("cgimage", "imageio"):
        rows = found.get(name)
        if not rows:
            continue
        counts: dict[float, int] = {}
        for size, _resident in rows:
            key = round(size, 1)
            counts[key] = counts.get(key, 0) + 1
        ranked = sorted(counts.items(), key=lambda item: (-item[0] * item[1], -item[0]))[:top]
        shapes = ",".join(f"{size:g}x{count}" for size, count in ranked)
        parts.append(f"{name}:{len(rows)}/{sum(res for _s, res in rows):.1f}[{shapes}]")
    return " ".join(parts)


def image_regions_line() -> str:
    """macOS: ``images=`` summary of the CGImage / ImageIO regions ('' elsewhere or without)."""
    if sys.platform != "darwin":
        return ""
    try:
        import resource

        summary = image_regions_summary(_darwin_regions(sizes=True), resource.getpagesize())
    except Exception:  # noqa: BLE001 - diagnostics only
        return ""
    return f"images={summary}" if summary else ""


def parse_debugmallocstats(text: str) -> dict[str, float]:
    """sys._debugmallocstats() output -> ``arenas``, ``arena_mb`` (all arenas), ``used_mb`` (live
    blocks), ``free_mb`` (free blocks in used pools), ``empty_pools_mb`` (unused pools inside
    arenas still held). Empty when the text is not recognised."""
    values: dict[str, int] = {}
    for line in str(text or "").splitlines():
        name, sep, number = line.rpartition("=")
        if not sep:
            continue
        try:
            value = int(number.strip().replace(",", ""))
        except ValueError:
            continue
        key = " ".join(name.strip().lstrip("#").split())
        if key.endswith("bytes/arena"):
            key = "arena bytes"
        elif "unused pools" in key and key.endswith("bytes"):
            key = "unused pool bytes"
        values.setdefault(key, value)
    if "arenas allocated current" not in values:
        return {}
    mb = 2**20
    return {
        "arenas": float(values["arenas allocated current"]),
        "arena_mb": round(values.get("arena bytes", 0) / mb, 1),
        "used_mb": round(values.get("bytes in allocated blocks", 0) / mb, 1),
        "free_mb": round(values.get("bytes in available blocks", 0) / mb, 1),
        "empty_pools_mb": round(values.get("unused pool bytes", 0) / mb, 1),
    }


_STDERR_LOCK = threading.Lock()


def obmalloc_stats() -> dict[str, float]:
    """Python's object allocator (arenas are anonymous mmap: in RSS, not in malloc zones).

    ``sys._debugmallocstats`` writes to C stderr, so fd 2 points at an anonymous temporary file
    for those few milliseconds (another thread's stderr text of that moment is lost)."""
    function = getattr(sys, "_debugmallocstats", None)
    if not callable(function):
        return {}
    import tempfile

    try:
        with _STDERR_LOCK, tempfile.TemporaryFile(mode="w+b") as capture:
            try:
                sys.stderr.flush()
            except Exception:  # noqa: BLE001
                pass
            saved = os.dup(2)
            try:
                os.dup2(capture.fileno(), 2)
                function()
            finally:
                os.dup2(saved, 2)
                os.close(saved)
            capture.seek(0)
            text = capture.read(1_000_000).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - diagnostics only
        return {}
    return parse_debugmallocstats(text)


def sqlite_memory() -> dict[str, float]:
    """MB SQLite's own allocator holds now / at most (page caches of open connections)."""
    try:
        import _sqlite3

        library = ctypes.CDLL(_sqlite3.__file__)
        used = library.sqlite3_memory_used
        highwater = library.sqlite3_memory_highwater
    except Exception:  # noqa: BLE001 - linked statically without exports, or no ctypes
        return {}
    try:
        used.restype = ctypes.c_int64
        highwater.restype = ctypes.c_int64
        highwater.argtypes = [ctypes.c_int]
        return {"used": round(used() / 2**20, 1), "highwater": round(highwater(0) / 2**20, 1)}
    except Exception:  # noqa: BLE001
        return {}


def vm_line() -> str:
    """``vm=footprint:…,internal:…,external:…,compressed:… regions=group:resident/dirty,…
    obmalloc=arenas:N/MB,used:…,free:…,empty:… sqlite_mb=used/highwater`` (parts it can tell)."""
    parts: list[str] = []
    vm = vm_info()
    if vm:
        parts.append("vm=" + ",".join(f"{key}:{value:.1f}" for key, value in vm.items()))
    regions = vm_regions()
    if regions:
        parts.append(
            "regions=" + ",".join(f"{key}:{res:.1f}/{dirty:.1f}" for key, (res, dirty) in regions.items())
        )
        if any(key in regions for key in IMAGE_TAGS.values()):
            images = image_regions_line()
            if images:
                parts.append(images)
    arenas = obmalloc_stats()
    if arenas:
        parts.append(
            f"obmalloc=arenas:{int(arenas['arenas'])}/{arenas['arena_mb']:.1f},"
            f"used:{arenas['used_mb']:.1f},free:{arenas['free_mb']:.1f},empty:{arenas['empty_pools_mb']:.1f}"
        )
    sqlite = sqlite_memory()
    if sqlite:
        parts.append(f"sqlite_mb={sqlite['used']:.1f}/{sqlite['highwater']:.1f}")
    return " ".join(parts)


def _length(value: Any) -> int | None:
    info = getattr(value, "cache_info", None)
    if callable(info) and hasattr(value, "cache_clear"):
        try:
            return int(info().currsize)
        except Exception:  # noqa: BLE001
            return None
    if isinstance(value, _CONTAINERS) and not isinstance(value, tuple):
        return len(value)
    if type(value).__name__ == "ndarray" and type(value).__module__ == "numpy":
        return int(value.size)
    return None


def _deep_size(value: Any, seen: set[int], budget: list[int]) -> int:
    """Approximate bytes held by ``value`` (containers, strings, bytes, arrays), shared objects
    once. A container of more than ``_SAMPLE_ENTRIES`` entries is sized from its first ones."""
    total = 0.0
    stack: list[tuple[Any, float]] = [(value, 1.0)]  # (object, how many objects it stands for)
    while stack and budget[0] > 0:
        item, weight = stack.pop()
        marker = id(item)
        if marker in seen:
            continue
        seen.add(marker)
        budget[0] -= 1
        nbytes = getattr(item, "nbytes", None)
        if type(item).__name__ == "ndarray" and isinstance(nbytes, int):
            total += nbytes * weight
            continue
        try:
            total += sys.getsizeof(item) * weight
        except TypeError:
            continue
        if isinstance(item, dict):
            sampled = len(item) > _SAMPLE_ENTRIES
            scale = weight * len(item) / _SAMPLE_ENTRIES if sampled else weight
            for key, child in itertools.islice(item.items(), _SAMPLE_ENTRIES) if sampled else item.items():
                stack.append((key, scale))
                stack.append((child, scale))
        elif isinstance(item, (list, tuple, set, frozenset, deque)):
            if len(item) > _SAMPLE_ENTRIES:
                scale = weight * len(item) / _SAMPLE_ENTRIES
                stack.extend((child, scale) for _index, child in zip(range(_SAMPLE_ENTRIES), item))
            else:
                stack.extend((child, weight) for child in item)
        elif (type(item).__module__ or "").startswith("pudge") and not callable(item):
            # Records kept in caches (dataclasses, cue objects).
            stack.extend((child, weight) for _name, child in _fields(item))
    return int(total)


def _fields(value: Any) -> list[tuple[str, Any]]:
    """Attributes of an object, ``__dict__`` and ``__slots__`` alike."""
    items: list[tuple[str, Any]] = []
    try:
        # A copy first (atomic under the GIL): services add attributes on other threads, and a
        # live dict changing size mid-iteration ended the whole release/report pass.
        items.extend(dict.copy(vars(value)).items())
    except TypeError:
        pass
    for klass in type(value).__mro__:
        for slot in getattr(klass, "__slots__", ()) or ():
            if isinstance(slot, str) and not slot.startswith("__") and hasattr(value, slot):
                items.append((slot, getattr(value, slot)))
    return items


def _is_service(value: Any) -> bool:
    if isinstance(value, (type, *_CONTAINERS, str, bytes, int, float, bool)) or value is None:
        return False
    module = type(value).__module__ or ""
    return module.startswith("pudge") and bool(_fields(value))


def _is_lru(value: Any) -> bool:
    return callable(getattr(value, "cache_info", None)) and hasattr(value, "cache_clear")


def iter_caches(api: Any = None):
    """(name, value) for every module-level and service-owned cache-like container."""
    for module_name, module in list(sys.modules.items()):
        if not module_name.startswith("pudge") or module is None:
            continue
        for attr, value in list(getattr(module, "__dict__", {}).items()):
            if attr.startswith("__"):
                continue
            if isinstance(value, type) and value.__module__ == module_name:
                for class_attr, class_value in list(vars(value).items()):
                    if class_attr.startswith("__") or isinstance(class_value, (tuple, frozenset)):
                        continue
                    if _length(class_value) is not None:
                        yield f"{module_name}.{attr}.{class_attr}", class_value
                continue
            if isinstance(value, (tuple, frozenset)):
                continue  # constants
            if _length(value) is not None:
                yield f"{module_name}.{attr}", value
    if api is None:
        return
    seen_services: set[int] = set()
    queue: list[tuple[str, Any, int]] = [("api", api, 0)]
    while queue:
        prefix, owner, depth = queue.pop(0)
        if id(owner) in seen_services:
            continue
        seen_services.add(id(owner))
        for attr, value in _fields(owner):
            if _length(value) is not None and not isinstance(value, tuple):
                yield f"{prefix}.{attr}", value
            elif depth < _SERVICE_DEPTH and _is_service(value) and not callable(value):
                queue.append((f"{prefix}.{attr}", value, depth + 1))


def cache_sizes(api: Any = None, *, deep: bool = True, minimum: int = 1) -> dict[str, tuple[int, float]]:
    """name -> (entries, approx MB). ``deep=False`` only counts entries (cheap)."""
    result: dict[str, tuple[int, float]] = {}
    seen: set[int] = set()
    reported: set[int] = set()
    # One budget for the whole walk (``seen`` is shared too): per cache it let one report
    # visit a million objects (a seen set of tens of MB) in the process it is meant to slim.
    budget = [_DEEP_TOTAL_BUDGET]
    for name, value in iter_caches(api):
        count = _length(value) or 0
        if count < minimum or id(value) in reported:
            continue  # one cache imported into several modules is listed once
        reported.add(id(value))
        size = 0.0
        if deep and not _is_lru(value):
            allowed = [min(_DEEP_BUDGET, budget[0])]
            try:
                size = _deep_size(value, seen, allowed) / 2**20 if allowed[0] > 0 else 0.0
            except (RuntimeError, ReferenceError):  # changed while read (another thread), a dead proxy
                size = 0.0
            budget[0] -= min(_DEEP_BUDGET, budget[0]) - allowed[0]
        result[name] = (count, round(size, 2))
    return result


_HEAVY_MODULES = (
    "numpy",
    "scipy",
    "PIL.Image",
    "Quartz",
    "ScreenCaptureKit",
    "mlx",
    "torch",
    "pudge.whisper_mlx",
)


def summary_line(api: Any = None, *, top: int = 12) -> str:
    """``rss_mb=… caches=name:entries/MB,…`` - the largest caches (≥ 0.1 MB or ≥ 1000 entries)."""
    sizes = cache_sizes(api, deep=True)
    ranked = sorted(
        ((name, value) for name, value in sizes.items() if value[1] >= 0.1 or value[0] >= 1000),
        key=lambda item: (-item[1][1], -item[1][0]),
    )[:top]
    caches = ",".join(f"{name.removeprefix('pudge.')}:{count}/{size:.1f}" for name, (count, size) in ranked)
    heavy = ",".join(name for name in _HEAVY_MODULES if name in sys.modules)
    return (
        f"rss_mb={rss_mb():.1f} rss_source={RSS_SOURCE} threads={threading.active_count()} gc_objects={len(gc.get_objects())} "
        f"modules={len(sys.modules)} heavy={heavy or '-'} caches={caches or '-'}"
    )


def heavy_origins() -> str:
    return ",".join(f"{name}@{origin}" for name, origin in sorted(IMPORT_ORIGINS.items())) or "-"


def object_types(*, top: int = 15, budget: int = 600_000) -> list[tuple[str, int, float]]:
    """(type name, objects, MB by sys.getsizeof) of the largest kinds of Python objects.

    The collector's objects (containers, instances) and, through their references, the untracked
    ones they hold (str, bytes, int, float). More than ``budget`` objects: every n-th container
    is looked at and the counts are scaled, so the numbers are estimates."""
    objects = gc.get_objects()
    step = max(1, len(objects) // max(1, budget))
    counts: dict[str, list[float]] = {}
    seen: set[int] = set()
    referents = 0

    def count(item: Any) -> None:
        kind = type(item)
        name = f"{kind.__module__}.{kind.__qualname__}" if kind.__module__ != "builtins" else kind.__name__
        try:
            size = sys.getsizeof(item)
        except TypeError:
            size = 0
        entry = counts.get(name)
        if entry is None:
            counts[name] = [step, size * step]
        else:
            entry[0] += step
            entry[1] += size * step

    for item in itertools.islice(objects, 0, None, step):
        count(item)
        if referents >= 4 * budget:
            continue
        for child in gc.get_referents(item):
            if gc.is_tracked(child) or id(child) in seen:
                continue
            seen.add(id(child))
            referents += 1
            count(child)
    del objects, seen
    ranked = sorted(counts.items(), key=lambda item: -item[1][1])[:top]
    return [(name, int(count), round(size / 2**20, 1)) for name, (count, size) in ranked]


class _MallocStatistics(ctypes.Structure):  # malloc_statistics_t
    _fields_ = [
        ("blocks_in_use", ctypes.c_uint),
        ("size_in_use", ctypes.c_size_t),
        ("max_size_in_use", ctypes.c_size_t),
        ("size_allocated", ctypes.c_size_t),
    ]


class _MallInfo2(ctypes.Structure):  # glibc struct mallinfo2
    _fields_ = [
        (name, ctypes.c_size_t)
        for name in (
            "arena",
            "ordblks",
            "smblks",
            "hblks",
            "hblkhd",
            "usmblks",
            "fsmblks",
            "uordblks",
            "fordblks",
            "keepcost",
        )
    ]


def malloc_stats() -> dict[str, float]:
    """C heap in MB: ``in_use`` (live blocks) vs ``allocated`` (held from the system); a big
    gap is fragmentation or freed memory not given back. Empty when the libc cannot tell."""
    try:
        if sys.platform == "darwin":
            libc = _libsystem()
            stats = _MallocStatistics()
            libc.malloc_zone_statistics.argtypes = [ctypes.c_void_p, ctypes.POINTER(_MallocStatistics)]
            libc.malloc_zone_statistics.restype = None
            libc.malloc_zone_statistics(None, ctypes.byref(stats))  # NULL: all zones summed
            in_use, allocated = stats.size_in_use, stats.size_allocated
        elif sys.platform.startswith("linux"):
            libc = ctypes.CDLL("libc.so.6")
            libc.mallinfo2.restype = _MallInfo2
            info = libc.mallinfo2()
            in_use, allocated = info.uordblks + info.hblkhd, info.arena + info.hblkhd
        else:
            return {}
    except Exception:  # noqa: BLE001 - diagnostics only
        return {}
    return {"in_use": round(in_use / 2**20, 1), "allocated": round(allocated / 2**20, 1)}


def heap_line() -> str:
    """``py_blocks=… malloc_in_use_mb=… malloc_allocated_mb=… types=name:count/MB,…``."""
    heap = malloc_stats()
    types = ",".join(f"{name}:{count}/{size:.1f}" for name, count, size in object_types())
    malloc = (
        f" malloc_in_use_mb={heap['in_use']:.1f} malloc_allocated_mb={heap['allocated']:.1f}" if heap else ""
    )
    return f"py_blocks={sys.getallocatedblocks()}{malloc} types={types or '-'}"


# ------------------------------------------------------------------ steps that grow memory

STEP_THRESHOLD_MB = 50.0


def _rss_bytes() -> int:
    """Cheap current RSS for step deltas (no ps fallback); 0 when unknown."""
    try:
        if sys.platform == "darwin":
            return _darwin_task_info_rss()
        if sys.platform.startswith("linux"):
            with open("/proc/self/statm", encoding="ascii") as handle:
                return int(handle.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    except Exception:  # noqa: BLE001
        pass
    return 0


@contextlib.contextmanager
def memory_step(logger: Any, step: str, **fields: object):
    """Logs ``EVENT memory_step step=… delta_mb=… rss_mb=… after_release_mb=…`` when the step
    left RSS more than ``STEP_THRESHOLD_MB`` higher (other threads may contribute: it names
    suspects, not proof). Such a step, and the big ones in ``RELEASE_AFTER_STEPS`` always, then
    collect garbage and hand freed C heap back to the system (``release_heap``)."""
    before = _rss_bytes()
    try:
        yield
    finally:
        if before:
            after = _rss_bytes()
            delta = (after - before) / 2**20
            grew = delta > STEP_THRESHOLD_MB
            released = None
            big = releases_after(step)
            if grew or big:
                released = release_heap(force=big)
            if grew and logger is not None:
                extra = "".join(f" {key}={value}" for key, value in fields.items())
                kept = f" after_release_mb={released[1]:.1f}" if released else ""
                footprint = footprint_mb()
                if footprint:
                    kept += f" footprint_mb={footprint:.1f}"
                try:
                    logger.info(
                        "EVENT memory_step step=%s delta_mb=%.1f rss_mb=%.1f%s thread=%s%s",
                        step,
                        delta,
                        after / 2**20,
                        kept,
                        threading.current_thread().name,
                        extra,
                    )
                except Exception:  # noqa: BLE001
                    pass


# Steps whose transient peak is known to be large: freed heap goes back right after them.
RELEASE_AFTER_STEPS = frozenset(
    {
        "anilist.library",
        "anilist.refresh_cache",
        "anilist.periodic_refresh",
        "statistics.query",
        "maintenance.total",
    }
)
# Every launch phase too (startup.phase.anilist, …): the launch peak goes back right away.
RELEASE_AFTER_PREFIXES = ("startup.phase.",)


def releases_after(step: str) -> bool:
    return step in RELEASE_AFTER_STEPS or str(step).startswith(RELEASE_AFTER_PREFIXES)


RELEASE_MIN_INTERVAL_SECONDS = 10.0
_RELEASE_LOCK = threading.Lock()
_LAST_RELEASE = [0.0]


def release_heap(*, force: bool = False) -> tuple[float, float] | None:
    """gc.collect() and malloc_trim / malloc_zone_pressure_relief: (rss MB before, after).

    Not more than once per RELEASE_MIN_INTERVAL_SECONDS unless ``force``; None when skipped
    (another thread is releasing, or one just ran)."""
    if not _RELEASE_LOCK.acquire(blocking=False):
        return None
    try:
        now = time.monotonic()
        if not force and now - _LAST_RELEASE[0] < RELEASE_MIN_INTERVAL_SECONDS:
            return None
        before = _rss_bytes() / 2**20
        gc.collect()
        _malloc_release()
        _LAST_RELEASE[0] = time.monotonic()
        return before, _rss_bytes() / 2**20
    finally:
        _RELEASE_LOCK.release()


# ------------------------------------------------------------------ who imports heavy modules

IMPORT_ORIGINS: dict[str, str] = {}


class _HeavyImportWatch:
    """A meta-path finder that only notes where a heavy module was first imported from."""

    def __init__(self, logger: Any) -> None:
        self.logger = logger

    def find_spec(self, name: str, path: Any = None, target: Any = None) -> None:
        if name in _HEAVY_MODULES and name not in IMPORT_ORIGINS and name not in sys.modules:
            origin = "?"
            frame = sys._getframe(1)
            while frame is not None:
                filename = frame.f_code.co_filename
                if "/pudge/" in filename and "memory_diagnostics" not in filename:
                    origin = f"{filename.rsplit('/pudge/', 1)[-1]}:{frame.f_lineno}"
                    break
                frame = frame.f_back
            IMPORT_ORIGINS[name] = origin
            if self.logger is not None:
                try:
                    self.logger.info(
                        "EVENT memory_import module=%s by=%s thread=%s",
                        name,
                        origin,
                        threading.current_thread().name,
                    )
                except Exception:  # noqa: BLE001
                    pass
        return None


def watch_heavy_imports(logger: Any) -> None:
    """Install the import watch once (later calls only update the logger)."""
    for finder in sys.meta_path:
        if isinstance(finder, _HeavyImportWatch):
            finder.logger = logger
            return
    for name in _HEAVY_MODULES:
        if name in sys.modules:
            IMPORT_ORIGINS.setdefault(name, "before_watch")
    sys.meta_path.insert(0, _HeavyImportWatch(logger))


def iter_services(api: Any):
    """The api object and the services it owns (``api.manager``, ``api.light_novels.…``)."""
    seen: set[int] = set()
    queue: list[tuple[Any, int]] = [(api, 0)]
    while queue:
        owner, depth = queue.pop(0)
        if id(owner) in seen:
            continue
        seen.add(id(owner))
        yield owner
        if depth >= _SERVICE_DEPTH:
            continue
        for _attr, value in _fields(owner):
            if _is_service(value) and not callable(value):
                queue.append((value, depth + 1))


def _malloc_release() -> None:
    """Give freed C heap back to the system where the allocator can (glibc / macOS zones)."""
    try:
        if sys.platform.startswith("linux"):
            ctypes.CDLL("libc.so.6").malloc_trim(0)
        elif sys.platform == "darwin":
            libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
            libc.malloc_zone_pressure_relief.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
            libc.malloc_zone_pressure_relief.restype = ctypes.c_size_t
            libc.malloc_zone_pressure_relief(None, 0)
    except Exception:  # noqa: BLE001 - not every libc has it
        pass


def release_idle_memory(api: Any = None, *, force: bool = False) -> dict[str, int]:
    """Let every service drop what it keeps only for speed and has not used for a while.

    A service opts in with ``release_idle(force: bool = False) -> int`` (entries dropped);
    ``force`` drops regardless of the idle time. Then garbage is collected and freed C heap is
    handed back to the system.
    """
    released: dict[str, int] = {}
    if api is not None:
        for service in iter_services(api):
            hook = getattr(service, "release_idle", None)
            if not callable(hook):
                continue
            try:
                dropped = int(hook(force=force) or 0)
            except Exception:  # noqa: BLE001 - a release must never break the app
                continue
            if dropped:
                name = type(service).__name__
                released[name] = released.get(name, 0) + dropped
    released["gc_objects"] = gc.collect()
    _malloc_release()
    _LAST_RELEASE[0] = time.monotonic()
    return released


def low_ram_line(api: Any = None) -> str:
    """``low_ram_mode=true|false`` and, when the window has an unloader, its page state."""
    power = getattr(getattr(api, "config", None), "power", None)
    text = f"low_ram_mode={str(bool(getattr(power, 'low_ram_mode', False))).lower()}"
    unloader = getattr(api, "_page_unloader", None) if api is not None else None
    if unloader is not None:
        text += (
            f" window_page={getattr(unloader, 'state', '?')}"
            f" page_unloads={getattr(unloader, 'unloads', 0)} page_reloads={getattr(unloader, 'reloads', 0)}"
            f" webview_parked={str(bool(getattr(unloader, 'parked', False))).lower()}"
        )
    host = getattr(api, "_window_process", None) if api is not None else None
    describe = getattr(host, "describe", None)
    if callable(describe):
        try:
            text += " " + str(describe())
        except Exception:  # noqa: BLE001 - diagnostics only
            pass
    return text


NS_WINDOW_OCCLUSION_STATE_VISIBLE = 1 << 1
WINDOW_STATE_WAIT_SECONDS = 1.0


def _read_window_state(native: Any) -> dict[str, int]:
    occlusion = int(native.occlusionState())
    return {
        "visible": int(bool(native.isVisible())),
        "occluded": int(not occlusion & NS_WINDOW_OCCLUSION_STATE_VISIBLE),
        "minimized": int(bool(native.isMiniaturized())),
    }


def window_line(api: Any = None, *, call_after: Any = None, wait: float = WINDOW_STATE_WAIT_SECONDS) -> str:
    """``window=visible:1,occluded:0,minimized:0`` of the main window, read on the main thread
    (``window=busy`` when it did not answer within ``wait``); '' without a native window."""
    window = getattr(api, "window", None) if api is not None else None
    native = getattr(window, "native", None)
    remote = getattr(window, "remote_state", None)
    if native is None and callable(remote):
        # Low RAM mode: the window lives in its own process, which reads it on its main thread.
        try:
            answer = dict(remote(wait) or {})
        except Exception:  # noqa: BLE001 - not answering: busy or gone
            return "window=busy"
        if not answer:
            return "window=closed"
        if "error" in answer:
            return f"window=error:{answer['error']}"
        return "window=" + ",".join(f"{key}:{value}" for key, value in answer.items())
    if native is None or not hasattr(native, "occlusionState"):
        return ""
    answer: dict[str, Any] = {}
    done = threading.Event()

    def read() -> None:
        try:
            answer.update(_read_window_state(native))
        except Exception as exc:  # noqa: BLE001 - diagnostics only
            answer["error"] = type(exc).__name__
        finally:
            done.set()

    try:
        if threading.current_thread() is threading.main_thread():
            read()
        else:
            if call_after is None:
                from PyObjCTools import AppHelper

                call_after = AppHelper.callAfter
            call_after(read)
            if not done.wait(wait):
                return "window=busy"
    except Exception:  # noqa: BLE001
        return ""
    if "error" in answer:
        return f"window=error:{answer['error']}"
    return "window=" + ",".join(f"{key}:{value}" for key, value in answer.items())


class MemoryReporter:
    """Every ``RELEASE_SECONDS`` lets idle caches go; every ``INTERVAL_SECONDS`` logs
    ``EVENT memory rss_mb=… caches=…``. Holds the api weakly (a closed api is not kept)."""

    INTERVAL_SECONDS = 3600.0
    RELEASE_SECONDS = 600.0

    def __init__(self, api: Any, logger: Any, *, interval: float | None = None) -> None:
        self._api = weakref.ref(api)
        self.logger = logger
        self.interval = float(interval or self.INTERVAL_SECONDS)

    def log_once(self, reason: str = "hourly") -> str:
        """Release idle memory first and log RSS before and after it, then the report."""
        api = self._api()
        before = rss_mb()
        try:
            release_idle_memory(api)
        except Exception:  # noqa: BLE001 - the report matters more
            pass
        after = rss_mb()
        try:
            where = vm_line()
        except Exception:  # noqa: BLE001 - diagnostics only
            where = ""
        try:
            shown = window_line(api)
        except Exception:  # noqa: BLE001 - diagnostics only
            shown = ""
        if shown:
            where = f"{where} {shown}".strip()
        try:
            from .js_bridge import BRIDGE_STATS

            bridge = BRIDGE_STATS.line() + " "
        except Exception:  # noqa: BLE001 - diagnostics only
            bridge = ""
        line = (
            f"{summary_line(api)} heavy_from={heavy_origins()} {heap_line()} {low_ram_line(api)} {bridge}"
            f"{where + ' ' if where else ''}rss_before_release_mb={before:.1f} rss_after_release_mb={after:.1f}"
        )

        del api
        if self.logger is not None:
            self.logger.info("EVENT memory reason=%s %s", reason, line)
        return line

    def run(self, stop: threading.Event) -> None:
        """The loop (``stop`` ends it): first line after the first interval."""
        next_log = time.monotonic() + self.interval
        step = min(self.RELEASE_SECONDS, self.interval)
        while not stop.wait(step):
            api = self._api()
            if api is None:
                return
            try:
                if time.monotonic() >= next_log:
                    del api
                    next_log = time.monotonic() + self.interval
                    self.log_once("hourly")  # releases first, logs before/after
                else:
                    release_idle_memory(api)
                    del api
            except Exception:  # noqa: BLE001 - diagnostics must never stop the app
                if self.logger is not None:
                    self.logger.exception("FAIL step=memory.report")
