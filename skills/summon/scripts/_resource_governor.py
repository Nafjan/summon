"""Hardware-aware dynamic resource governor and per-user host lease registry.

Provides cross-platform stdlib probes (Windows GlobalMemoryStatusEx /
GetSystemTimes, Linux /proc/meminfo + getloadavg, macOS vm_stat + getloadavg,
and dual-volume shutil.disk_usage) plus a lock-protected per-user host
reservation registry so concurrent foreground manifests and background missions
share machine capacity without violating RAM or disk safety floors.
"""

from __future__ import annotations

from contextlib import contextmanager
import ctypes
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

import _jobs

SCHEMA = "summon.resource-governor/v1"
LEASE_REGISTRY_SCHEMA = "summon.host-resource-leases/v1"

DEFAULT_MIN_FREE_RAM_MB = 3072
DEFAULT_WORKER_RAM_ESTIMATE_MB = 512
DEFAULT_MAX_CPU_PCT = 85.0
DEFAULT_MAX_LOAD_RATIO = 0.85
DEFAULT_MIN_FREE_DISK_GB = 2.0
DEFAULT_MAX_AUTO_WORKERS = 16
DEFAULT_LEASE_TTL_SEC = 3600.0
MAX_LEASE_LIFETIME_SEC = 86_400.0
DEFAULT_RESOURCE_WAIT_TIMEOUT_SEC = 60.0
PENDING_RAM_WINDOW_SEC = 10.0
MAX_LEASES = 256
MAX_REGISTRY_BYTES = 1024 * 1024

_LEASE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_CPU_SAMPLE_LOCK = threading.Lock()
_LAST_WIN_CPU_TIMES: tuple[int, int, int, float] | None = None
_LAST_LINUX_CPU_TIMES: tuple[int, int, float] | None = None


class ResourceGovernorError(PermissionError):
    """Raised when the host resource lease registry or secret is tampered or corrupted."""

    def __init__(self, message: str, *, error_kind: str = "resource_registry_corrupt") -> None:
        super().__init__(message)
        self.error_kind = error_kind


class ResourceExhaustedError(RuntimeError):
    """Raised when host resources remain below hard safety floors with zero active workers."""

    def __init__(self, reason: str, snapshot: dict[str, Any]) -> None:
        super().__init__(reason)
        self.reason = reason
        self.error_kind = "host_resources_exhausted"
        self.snapshot = snapshot


def _env_int(name: str, default: int, minimum: int = 1, maximum: int = 1_048_576) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        val = int(raw.strip())
    except ValueError:
        return default
    return max(minimum, min(maximum, val))


def _env_float(name: str, default: float, minimum: float = 0.0, maximum: float = 1_000_000.0) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        val = float(raw.strip())
    except ValueError:
        return default
    if not math.isfinite(val):
        return default
    return max(minimum, min(maximum, val))


def process_birth_token(pid: int) -> str | None:
    """Return a best-effort OS process birth token for PID reuse detection."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    if os.name == "nt":
        try:
            from ctypes import wintypes

            class _FILETIME(ctypes.Structure):
                _fields_ = [("dwLowDateTime", wintypes.DWORD),
                            ("dwHighDateTime", wintypes.DWORD)]

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            open_proc = kernel32.OpenProcess
            open_proc.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            open_proc.restype = wintypes.HANDLE
            get_times = kernel32.GetProcessTimes
            get_times.argtypes = (
                wintypes.HANDLE,
                ctypes.POINTER(_FILETIME),
                ctypes.POINTER(_FILETIME),
                ctypes.POINTER(_FILETIME),
                ctypes.POINTER(_FILETIME),
            )
            get_times.restype = wintypes.BOOL
            close_h = kernel32.CloseHandle
            close_h.argtypes = (wintypes.HANDLE,)
            close_h.restype = wintypes.BOOL

            handle = open_proc(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return None
            try:
                created = _FILETIME()
                exited = _FILETIME()
                kernel = _FILETIME()
                user = _FILETIME()
                if not get_times(handle, ctypes.byref(created), ctypes.byref(exited),
                                 ctypes.byref(kernel), ctypes.byref(user)):
                    return None
                ticks = (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)
                return f"win32:{pid}:{ticks}"
            finally:
                close_h(handle)
        except Exception:  # noqa: BLE001
            return None
    proc_stat = Path(f"/proc/{pid}/stat")
    if proc_stat.exists():
        try:
            raw = proc_stat.read_text(encoding="utf-8", errors="replace")
            rparen = raw.rfind(")")
            if rparen != -1:
                fields = raw[rparen + 2:].split()
                if len(fields) > 19:
                    return f"proc:{pid}:{fields[19]}"
        except OSError:
            pass
    return None


def _probe_windows_memory() -> dict[str, Any]:
    try:
        from ctypes import wintypes

        class _MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", wintypes.DWORD),
                ("dwMemoryLoad", wintypes.DWORD),
                ("ullTotalPhys", ctypes.c_uint64),
                ("ullAvailPhys", ctypes.c_uint64),
                ("ullTotalPageFile", ctypes.c_uint64),
                ("ullAvailPageFile", ctypes.c_uint64),
                ("ullTotalVirtual", ctypes.c_uint64),
                ("ullAvailVirtual", ctypes.c_uint64),
                ("ullAvailExtendedVirtual", ctypes.c_uint64),
            ]

        stat_ex = _MEMORYSTATUSEX()
        stat_ex.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        fn = kernel32.GlobalMemoryStatusEx
        fn.argtypes = (ctypes.POINTER(_MEMORYSTATUSEX),)
        fn.restype = wintypes.BOOL
        if not fn(ctypes.byref(stat_ex)):
            return {
                "status": "error",
                "source": "win32.GlobalMemoryStatusEx",
                "unit": "MB",
                "total_ram_mb": None,
                "avail_ram_mb": None,
                "memory_load_pct": None,
                "sample_age_ms": 0,
            }
        total_mb = int(stat_ex.ullTotalPhys // (1024 * 1024))
        avail_mb = int(stat_ex.ullAvailPhys // (1024 * 1024))
        if total_mb <= 0 or avail_mb < 0 or avail_mb > total_mb:
            return {
                "status": "error",
                "source": "win32.GlobalMemoryStatusEx",
                "unit": "MB",
                "total_ram_mb": None,
                "avail_ram_mb": None,
                "memory_load_pct": None,
                "sample_age_ms": 0,
            }
        return {
            "status": "ok",
            "source": "win32.GlobalMemoryStatusEx",
            "unit": "MB",
            "total_ram_mb": total_mb,
            "avail_ram_mb": avail_mb,
            "memory_load_pct": float(stat_ex.dwMemoryLoad),
            "sample_age_ms": 0,
        }
    except Exception:  # noqa: BLE001
        return {
            "status": "error",
            "source": "win32.GlobalMemoryStatusEx",
            "unit": "MB",
            "total_ram_mb": None,
            "avail_ram_mb": None,
            "memory_load_pct": None,
            "sample_age_ms": 0,
        }


def _parse_linux_meminfo(text: str) -> dict[str, Any]:
    values_kb: dict[str, int] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        k, _, rest = line.partition(":")
        parts = rest.strip().split()
        if not parts:
            continue
        try:
            values_kb[k.strip()] = int(parts[0])
        except ValueError:
            continue
    total_kb = values_kb.get("MemTotal")
    avail_kb = values_kb.get("MemAvailable")
    source = "proc.meminfo"
    if avail_kb is None:
        free_kb = values_kb.get("MemFree")
        buffers_kb = values_kb.get("Buffers", 0)
        cached_kb = values_kb.get("Cached", 0)
        if free_kb is not None:
            avail_kb = free_kb + buffers_kb + cached_kb
            source = "proc.meminfo.fallback"
    if total_kb is None or avail_kb is None or total_kb <= 0 or avail_kb < 0:
        return {
            "status": "unavailable",
            "source": source,
            "unit": "MB",
            "total_ram_mb": None,
            "avail_ram_mb": None,
            "memory_load_pct": None,
            "sample_age_ms": 0,
        }
    total_mb = max(1, total_kb // 1024)
    avail_mb = min(total_mb, max(0, avail_kb // 1024))
    load_pct = round(max(0.0, min(100.0, (1.0 - (avail_mb / total_mb)) * 100.0)), 1)
    return {
        "status": "ok",
        "source": source,
        "unit": "MB",
        "total_ram_mb": total_mb,
        "avail_ram_mb": avail_mb,
        "memory_load_pct": load_pct,
        "sample_age_ms": 0,
    }


def _parse_darwin_vm_stat(vm_stat_text: str, total_bytes: int | None) -> dict[str, Any]:
    page_size = 4096
    m = re.search(r"page size of (\d+) bytes", vm_stat_text)
    if m:
        try:
            page_size = int(m.group(1))
        except ValueError:
            page_size = 4096
    pages: dict[str, int] = {}
    for line in vm_stat_text.splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        num_str = v.strip().rstrip(".")
        try:
            pages[k.strip()] = int(num_str)
        except ValueError:
            continue
    if "Pages free" not in pages:
        return {
            "status": "unavailable",
            "source": "darwin.vm_stat",
            "unit": "MB",
            "total_ram_mb": None,
            "avail_ram_mb": None,
            "memory_load_pct": None,
            "sample_age_ms": 0,
        }
    avail_pages = (
        pages.get("Pages free", 0)
        + pages.get("Pages inactive", 0)
        + pages.get("Pages speculative", 0)
    )
    avail_mb = max(0, (avail_pages * page_size) // (1024 * 1024))
    total_mb = int(total_bytes // (1024 * 1024)) if total_bytes and total_bytes > 0 else None
    if total_mb is not None and avail_mb > total_mb:
        avail_mb = total_mb
    load_pct = (
        round(max(0.0, min(100.0, (1.0 - (avail_mb / total_mb)) * 100.0)), 1)
        if total_mb and total_mb > 0
        else None
    )
    return {
        "status": "ok",
        "source": "darwin.vm_stat",
        "unit": "MB",
        "total_ram_mb": total_mb,
        "avail_ram_mb": avail_mb,
        "memory_load_pct": load_pct,
        "sample_age_ms": 0,
    }


def probe_memory() -> dict[str, Any]:
    """Probe physical memory across Windows, Linux, and macOS using stdlib only."""
    if os.name == "nt":
        return _probe_windows_memory()
    meminfo = Path("/proc/meminfo")
    if meminfo.exists():
        try:
            return _parse_linux_meminfo(meminfo.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            pass
    if sys.platform == "darwin":
        try:
            total_bytes = None
            if hasattr(os, "sysconf"):
                try:
                    ps = int(os.sysconf("SC_PAGE_SIZE"))
                    pp = int(os.sysconf("SC_PHYS_PAGES"))
                    if ps > 0 and pp > 0:
                        total_bytes = ps * pp
                except (ValueError, OSError):
                    total_bytes = None
            out = subprocess.check_output(["vm_stat"], text=True, timeout=2.0)
            return _parse_darwin_vm_stat(out, total_bytes)
        except Exception:  # noqa: BLE001
            pass
    return {
        "status": "unavailable",
        "source": "none",
        "unit": "MB",
        "total_ram_mb": None,
        "avail_ram_mb": None,
        "memory_load_pct": None,
        "sample_age_ms": 0,
    }


def _read_win_system_times() -> tuple[int, int, int] | None:
    try:
        from ctypes import wintypes

        class _FILETIME(ctypes.Structure):
            _fields_ = [("dwLowDateTime", wintypes.DWORD),
                        ("dwHighDateTime", wintypes.DWORD)]

        idle = _FILETIME()
        kernel = _FILETIME()
        user = _FILETIME()
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        fn = kernel32.GetSystemTimes
        fn.argtypes = (
            ctypes.POINTER(_FILETIME),
            ctypes.POINTER(_FILETIME),
            ctypes.POINTER(_FILETIME),
        )
        fn.restype = wintypes.BOOL
        if not fn(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
            return None
        idle_v = (int(idle.dwHighDateTime) << 32) | int(idle.dwLowDateTime)
        kernel_v = (int(kernel.dwHighDateTime) << 32) | int(kernel.dwLowDateTime)
        user_v = (int(user.dwHighDateTime) << 32) | int(user.dwLowDateTime)
        return idle_v, kernel_v, user_v
    except Exception:  # noqa: BLE001
        return None


def compute_win_cpu_busy_pct(
    sample1: tuple[int, int, int],
    sample2: tuple[int, int, int],
) -> float | None:
    """Compute Windows CPU busy percentage from two (idle, kernel, user) samples.

    On Windows, GetSystemTimes ``lpKernelTime`` includes ``lpIdleTime``, so:
        total = d_kernel + d_user
        busy  = (d_kernel - d_idle) + d_user = total - d_idle
    """
    d_idle = sample2[0] - sample1[0]
    d_kernel = sample2[1] - sample1[1]
    d_user = sample2[2] - sample1[2]
    if d_idle < 0 or d_kernel < 0 or d_user < 0:
        return None
    total = d_kernel + d_user
    if total <= 0 or d_idle > total:
        return None
    busy = total - d_idle
    return round(max(0.0, min(100.0, (busy / total) * 100.0)), 1)


def _read_linux_proc_stat() -> tuple[int, int] | None:
    """Return ``(idle_jiffies, total_jiffies)`` from ``/proc/stat`` aggregate ``cpu`` line."""
    proc_stat = Path("/proc/stat")
    if not proc_stat.exists():
        return None
    try:
        for line in proc_stat.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("cpu "):
                parts = [int(x) for x in line.split()[1:9] if x.isdigit()]
                if len(parts) >= 4:
                    idle = parts[3] + (parts[4] if len(parts) > 4 else 0)
                    total = sum(parts)
                    if total > 0 and idle >= 0:
                        return idle, total
                break
    except (OSError, ValueError):
        pass
    return None


def compute_linux_cpu_busy_pct(
    sample1: tuple[int, int],
    sample2: tuple[int, int],
) -> float | None:
    """Compute Linux CPU busy percentage from two ``(idle, total)`` jiffy samples."""
    d_idle = sample2[0] - sample1[0]
    d_total = sample2[1] - sample1[1]
    if d_idle < 0 or d_total <= 0 or d_idle > d_total:
        return None
    busy = d_total - d_idle
    return round(max(0.0, min(100.0, (busy / d_total) * 100.0)), 1)


def probe_cpu(*, sample_delay_sec: float = 0.05) -> dict[str, Any]:
    """Probe CPU busy percentage (Windows, Linux /proc/stat) and normalized load ratio (POSIX)."""
    global _LAST_WIN_CPU_TIMES, _LAST_LINUX_CPU_TIMES
    cpu_count = max(1, os.cpu_count() or 1)
    if os.name == "nt":
        now = time.monotonic()
        with _CPU_SAMPLE_LOCK:
            prev = _LAST_WIN_CPU_TIMES
            cur = _read_win_system_times()
            if cur is None:
                return {
                    "status": "error",
                    "source": "win32.GetSystemTimes",
                    "unit": "percent",
                    "cpu_count": cpu_count,
                    "cpu_busy_pct": None,
                    "normalized_load_ratio": None,
                    "sample_age_ms": 0,
                }
            if prev is None or (now - prev[3]) > 30.0 or cur == prev[:3]:
                if sample_delay_sec > 0:
                    time.sleep(min(0.2, max(0.01, sample_delay_sec)))
                cur2 = _read_win_system_times()
                if cur2 is None:
                    return {
                        "status": "error",
                        "source": "win32.GetSystemTimes",
                        "unit": "percent",
                        "cpu_count": cpu_count,
                        "cpu_busy_pct": None,
                        "normalized_load_ratio": None,
                        "sample_age_ms": 0,
                    }
                pct = compute_win_cpu_busy_pct(cur, cur2)
                _LAST_WIN_CPU_TIMES = (cur2[0], cur2[1], cur2[2], time.monotonic())
            else:
                pct = compute_win_cpu_busy_pct((prev[0], prev[1], prev[2]), cur)
                _LAST_WIN_CPU_TIMES = (cur[0], cur[1], cur[2], now)
        if pct is None:
            return {
                "status": "unavailable",
                "source": "win32.GetSystemTimes",
                "unit": "percent",
                "cpu_count": cpu_count,
                "cpu_busy_pct": None,
                "normalized_load_ratio": None,
                "sample_age_ms": 0,
            }
        return {
            "status": "ok",
            "source": "win32.GetSystemTimes",
            "unit": "percent",
            "cpu_count": cpu_count,
            "cpu_busy_pct": pct,
            "normalized_load_ratio": round(pct / 100.0, 3),
            "sample_age_ms": 0,
        }

    linux_pct: float | None = None
    cur_linux = _read_linux_proc_stat()
    if cur_linux is not None:
        now = time.monotonic()
        with _CPU_SAMPLE_LOCK:
            prev_l = _LAST_LINUX_CPU_TIMES
            if prev_l is None or (now - prev_l[2]) > 30.0 or cur_linux == prev_l[:2]:
                if sample_delay_sec > 0:
                    time.sleep(min(0.2, max(0.01, sample_delay_sec)))
                cur_linux2 = _read_linux_proc_stat()
                if cur_linux2 is not None:
                    linux_pct = compute_linux_cpu_busy_pct(cur_linux, cur_linux2)
                    _LAST_LINUX_CPU_TIMES = (cur_linux2[0], cur_linux2[1], time.monotonic())
            else:
                linux_pct = compute_linux_cpu_busy_pct((prev_l[0], prev_l[1]), cur_linux)
                _LAST_LINUX_CPU_TIMES = (cur_linux[0], cur_linux[1], now)

    if hasattr(os, "getloadavg"):
        try:
            load1, _, _ = os.getloadavg()
            if not math.isfinite(load1) or load1 < 0:
                raise ValueError("invalid loadavg")
            ratio = round(load1 / cpu_count, 3)
            return {
                "status": "ok",
                "source": "proc.stat+getloadavg" if linux_pct is not None else "posix.getloadavg",
                "unit": "percent" if linux_pct is not None else "ratio",
                "cpu_count": cpu_count,
                "cpu_busy_pct": linux_pct,
                "loadavg_1m": round(load1, 2),
                "normalized_load_ratio": ratio,
                "sample_age_ms": 0,
            }
        except (OSError, ValueError):
            pass
    if linux_pct is not None:
        return {
            "status": "ok",
            "source": "proc.stat",
            "unit": "percent",
            "cpu_count": cpu_count,
            "cpu_busy_pct": linux_pct,
            "normalized_load_ratio": round(linux_pct / 100.0, 3),
            "sample_age_ms": 0,
        }
    return {
        "status": "unavailable",
        "source": "none",
        "unit": "ratio",
        "cpu_count": cpu_count,
        "cpu_busy_pct": None,
        "normalized_load_ratio": None,
        "sample_age_ms": 0,
    }


def probe_disk(cwd: str | None = None, job_root: str | None = None) -> dict[str, Any]:
    """Check free disk space across both target workspace and job/worktree store volumes."""
    cwd_path = os.path.abspath(cwd or os.getcwd())
    root_path = _jobs.resolve_jobs_dir(job_root)
    free_values: list[float] = []
    cwd_free_gb: float | None = None
    job_root_free_gb: float | None = None

    for candidate, is_cwd in ((cwd_path, True), (root_path, False)):
        probe_target = candidate
        while probe_target and not os.path.exists(probe_target):
            parent = os.path.dirname(probe_target)
            if parent == probe_target:
                break
            probe_target = parent
        try:
            usage = shutil.disk_usage(probe_target)
            gb = round(usage.free / (1024 ** 3), 2)
            free_values.append(gb)
            if is_cwd:
                cwd_free_gb = gb
            else:
                job_root_free_gb = gb
        except OSError:
            continue

    if not free_values:
        return {
            "status": "unavailable",
            "source": "shutil.disk_usage",
            "unit": "GB",
            "cwd_free_gb": None,
            "job_root_free_gb": None,
            "effective_free_gb": None,
            "sample_age_ms": 0,
        }
    return {
        "status": "ok",
        "source": "shutil.disk_usage",
        "unit": "GB",
        "cwd_free_gb": cwd_free_gb,
        "job_root_free_gb": job_root_free_gb,
        "effective_free_gb": min(free_values),
        "sample_age_ms": 0,
    }


def _registry_paths(job_root: str | None = None) -> tuple[str, str, str]:
    root = _jobs.resolve_jobs_dir(job_root)
    _jobs.ensure_jobs_dir(root)
    reg = os.path.join(root, ".host_resource_leases.json")
    lock = os.path.join(root, ".host_resource_leases.lock")
    key_file = os.path.join(root, ".host_resource_leases.secret")
    return reg, lock, key_file


def _load_or_create_secret(key_file: str) -> bytes:
    if os.path.islink(key_file):
        raise ResourceGovernorError(
            "host resource lease secret cannot be a symlink",
            error_kind="resource_secret_symlink",
        )
    try:
        fd = os.open(key_file, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise ResourceGovernorError(
                    "host resource lease secret is not a regular file",
                    error_kind="resource_secret_invalid",
                )
            raw = os.read(fd, 128).strip()
            if len(raw) == 64 and all(c in b"0123456789abcdef" for c in raw):
                return bytes.fromhex(raw.decode("ascii"))
            raise ResourceGovernorError(
                "host resource lease secret is truncated or malformed",
                error_kind="resource_secret_corrupt",
            )
        finally:
            os.close(fd)
    except FileNotFoundError:
        pass
    secret = secrets.token_bytes(32)
    d = os.path.dirname(os.path.abspath(key_file))
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".summon-rkey-", suffix=".tmp")
    try:
        if os.name != "nt":
            try:
                os.fchmod(fd, 0o600)
            except OSError:
                pass
        with os.fdopen(fd, "wb") as fh:
            fh.write(secret.hex().encode("ascii"))
            fh.flush()
            os.fsync(fh.fileno())
        _jobs._replace_with_retry(tmp, key_file)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return secret


def _canonical_json(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _sign_registry(payload: dict[str, Any], secret: bytes) -> str:
    unsigned = {k: v for k, v in payload.items() if k != "mac"}
    return hmac.new(secret, _canonical_json(unsigned), hashlib.sha256).hexdigest()


@contextmanager
def _registry_lock(lock_path: str, timeout_sec: float = 5.0):
    token = f"{os.getpid()}:{uuid.uuid4().hex}"
    deadline = time.monotonic() + timeout_sec
    fd = None
    while fd is None:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except (FileExistsError, PermissionError):
            try:
                st = os.stat(lock_path)
                if (time.time() - st.st_mtime) > 30.0:
                    try:
                        with open(lock_path, encoding="ascii", errors="replace") as fh:
                            holder = fh.read().strip()
                        holder_pid = int(holder.split(":", 1)[0])
                        if _jobs._pid_liveness(holder_pid) == "dead":
                            trash = f"{lock_path}.stale.{os.getpid()}.{uuid.uuid4().hex}"
                            os.replace(lock_path, trash)
                            try:
                                os.unlink(trash)
                            except OSError:
                                pass
                            continue
                    except (OSError, ValueError):
                        pass
            except OSError:
                pass
            if time.monotonic() >= deadline:
                raise TimeoutError("host resource lease registry lock timed out")
            time.sleep(0.01)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as fh:
            fh.write(token)
            fh.flush()
        yield
    finally:
        try:
            with open(lock_path, encoding="ascii") as fh:
                current = fh.read().strip()
            if current == token:
                _jobs._unlink_with_retry(lock_path) if hasattr(_jobs, "_unlink_with_retry") else os.unlink(lock_path)
        except OSError:
            pass


def _read_registry_unlocked(reg_path: str, secret: bytes) -> dict[str, Any]:
    if not os.path.lexists(reg_path):
        return {"schema": LEASE_REGISTRY_SCHEMA, "generation": 0, "leases": {}}
    if os.path.islink(reg_path):
        raise ResourceGovernorError(
            "host resource lease registry cannot be a symlink",
            error_kind="resource_registry_symlink",
        )
    try:
        with open(reg_path, "rb") as fh:
            raw = fh.read(MAX_REGISTRY_BYTES + 1)
    except OSError as exc:
        raise ResourceGovernorError(
            f"cannot read host resource lease registry: {exc}",
            error_kind="resource_registry_unreadable",
        ) from exc
    if len(raw) > MAX_REGISTRY_BYTES:
        raise ResourceGovernorError(
            f"host resource lease registry exceeds {MAX_REGISTRY_BYTES} bytes",
            error_kind="resource_registry_oversize",
        )
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ResourceGovernorError(
            f"host resource lease registry is malformed JSON: {exc}",
            error_kind="resource_registry_corrupt",
        ) from exc
    if (not isinstance(data, dict)
            or data.get("schema") != LEASE_REGISTRY_SCHEMA
            or not isinstance(data.get("leases"), dict)
            or not isinstance(data.get("generation"), int)):
        raise ResourceGovernorError(
            "host resource lease registry has invalid schema or structure",
            error_kind="resource_registry_invalid_schema",
        )
    expected_mac = _sign_registry(data, secret)
    got_mac = data.get("mac")
    if not isinstance(got_mac, str) or not hmac.compare_digest(expected_mac, got_mac):
        raise ResourceGovernorError(
            "host resource lease registry HMAC verification failed",
            error_kind="resource_registry_mac_invalid",
        )
    return data


def _write_registry_unlocked(reg_path: str, data: dict[str, Any], secret: bytes) -> None:
    payload = {
        "schema": LEASE_REGISTRY_SCHEMA,
        "generation": int(data.get("generation", 0)) + 1,
        "leases": dict(data.get("leases") or {}),
    }
    payload["mac"] = _sign_registry(payload, secret)
    _jobs._atomic_write_json(reg_path, payload)


def _is_lease_live(lease: Mapping[str, Any], now_wall: float) -> bool:
    if not isinstance(lease, Mapping):
        return False
    pid = lease.get("owner_pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    liveness = _jobs._pid_liveness(pid)
    if liveness == "dead":
        return False
    recorded_birth = lease.get("owner_birth")
    birth_verified = False
    if isinstance(recorded_birth, str) and recorded_birth:
        current_birth = process_birth_token(pid)
        if current_birth is not None:
            if current_birth != recorded_birth:
                return False
            birth_verified = True
    created_at = lease.get("created_at")
    if isinstance(created_at, (int, float)) and math.isfinite(created_at):
        if (now_wall - float(created_at)) > MAX_LEASE_LIFETIME_SEC:
            return False
    expires_at = lease.get("expires_at")
    if not isinstance(expires_at, (int, float)) or not math.isfinite(expires_at):
        return False
    if now_wall >= expires_at:
        # Keep lease alive past initial TTL only when owner PID is confirmed alive with matching birth token
        if liveness == "alive" and birth_verified:
            return True
        return False
    return True


def reconcile_leases(
    job_root: str | None = None,
    *,
    now_wall: float | None = None,
) -> dict[str, Any]:
    """Reap expired or dead-owner leases and return active lease summary."""
    now = now_wall if now_wall is not None else time.time()
    reg_path, lock_path, key_file = _registry_paths(job_root)
    with _registry_lock(lock_path):
        secret = _load_or_create_secret(key_file)
        reg = _read_registry_unlocked(reg_path, secret)
        leases = reg.get("leases") or {}
        live_leases: dict[str, dict[str, Any]] = {}
        reaped = 0
        for lid, item in leases.items():
            if _LEASE_ID_RE.fullmatch(str(lid)) and _is_lease_live(item, now):
                live_leases[str(lid)] = dict(item)
            else:
                reaped += 1
        if reaped > 0:
            reg["leases"] = live_leases
            _write_registry_unlocked(reg_path, reg, secret)
    return _summarize_leases(live_leases, now)


def _summarize_leases(leases: Mapping[str, Mapping[str, Any]], now_wall: float) -> dict[str, Any]:
    active_count = len(leases)
    pending_ram_mb = 0
    by_backend: dict[str, int] = {}
    for item in leases.values():
        state = str(item.get("state") or "reserved")
        created_at = float(item.get("created_at") or 0.0)
        ram_est = int(item.get("reserved_ram_mb") or DEFAULT_WORKER_RAM_ESTIMATE_MB)
        # Only subtract reserved_ram_mb for reservations not yet reflected in OS RSS
        # (state in reserved/launching, or transitioned to running within PENDING_RAM_WINDOW_SEC).
        if state in ("reserved", "launching") or (now_wall - created_at) < PENDING_RAM_WINDOW_SEC:
            pending_ram_mb += max(0, ram_est)
        backend = str(item.get("backend") or "default")
        by_backend[backend] = by_backend.get(backend, 0) + 1
    return {
        "active_leases": active_count,
        "pending_reserved_ram_mb": pending_ram_mb,
        "by_backend": by_backend,
    }


def evaluate_admission(
    *,
    memory: Mapping[str, Any],
    cpu: Mapping[str, Any],
    disk: Mapping[str, Any],
    lease_summary: Mapping[str, Any],
    min_free_ram_mb: int | None = None,
    worker_ram_estimate_mb: int | None = None,
    max_cpu_pct: float | None = None,
    max_load_ratio: float | None = None,
    min_free_disk_gb: float | None = None,
    max_auto_workers: int | None = None,
) -> dict[str, Any]:
    """Pure policy evaluator: computes additional_slots and recommended_total_slots.

    Strictly enforces 0 new admissions when below RAM or disk safety floors.
    """
    min_ram = (min_free_ram_mb if min_free_ram_mb is not None
               else _env_int("SUMMON_MIN_FREE_RAM_MB", DEFAULT_MIN_FREE_RAM_MB, 256, 524_288))
    worker_ram = (worker_ram_estimate_mb if worker_ram_estimate_mb is not None
                  else _env_int("SUMMON_WORKER_RAM_ESTIMATE_MB", DEFAULT_WORKER_RAM_ESTIMATE_MB, 64, 65_536))
    cpu_cap = (max_cpu_pct if max_cpu_pct is not None
               else _env_float("SUMMON_MAX_CPU_PCT", DEFAULT_MAX_CPU_PCT, 10.0, 100.0))
    load_cap = (max_load_ratio if max_load_ratio is not None
                else _env_float("SUMMON_MAX_LOAD_RATIO", DEFAULT_MAX_LOAD_RATIO, 0.1, 10.0))
    disk_floor = (min_free_disk_gb if min_free_disk_gb is not None
                  else _env_float("SUMMON_MIN_FREE_DISK_GB", DEFAULT_MIN_FREE_DISK_GB, 0.1, 10_000.0))
    max_workers = (max_auto_workers if max_auto_workers is not None
                   else _env_int("SUMMON_MAX_AUTO_WORKERS", DEFAULT_MAX_AUTO_WORKERS, 1, 64))

    active_leases = max(0, int(lease_summary.get("active_leases") or 0))
    pending_ram = max(0, int(lease_summary.get("pending_reserved_ram_mb") or 0))

    avail_ram = memory.get("avail_ram_mb")
    effective_avail_ram = (
        max(0, int(avail_ram) - pending_ram)
        if isinstance(avail_ram, int) and not isinstance(avail_ram, bool)
        else None
    )
    free_disk = disk.get("effective_free_gb")
    cpu_count = max(1, int(cpu.get("cpu_count") or 1))
    cpu_busy = cpu.get("cpu_busy_pct")
    load_ratio = cpu.get("normalized_load_ratio")

    # 1. Hard Disk Floor (0 admissions allowed below floor)
    if isinstance(free_disk, (int, float)) and free_disk < disk_floor:
        return {
            "schema": SCHEMA,
            "admitted": False,
            "pressure": "disk_floor",
            "floor_blocked": "disk_floor",
            "reason": f"free disk {free_disk:.2f} GB is below safety floor {disk_floor:.2f} GB",
            "active_leases": active_leases,
            "pending_reserved_ram_mb": pending_ram,
            "effective_avail_ram_mb": effective_avail_ram,
            "additional_slots": 0,
            "recommended_total_slots": active_leases,
            "max_auto_workers": max_workers,
            "limits": {
                "min_free_ram_mb": min_ram,
                "worker_ram_estimate_mb": worker_ram,
                "max_cpu_pct": cpu_cap,
                "max_load_ratio": load_cap,
                "min_free_disk_gb": disk_floor,
            },
            "memory": dict(memory),
            "cpu": dict(cpu),
            "disk": dict(disk),
        }

    # 2. Hard RAM Floor (0 admissions allowed if one more worker breaches floor)
    if effective_avail_ram is not None and (effective_avail_ram - worker_ram) < min_ram:
        return {
            "schema": SCHEMA,
            "admitted": False,
            "pressure": "ram_floor",
            "floor_blocked": "ram_floor",
            "reason": (f"effective available RAM {effective_avail_ram} MB minus worker estimate "
                       f"{worker_ram} MB is below safety floor {min_ram} MB"),
            "active_leases": active_leases,
            "pending_reserved_ram_mb": pending_ram,
            "effective_avail_ram_mb": effective_avail_ram,
            "additional_slots": 0,
            "recommended_total_slots": active_leases,
            "max_auto_workers": max_workers,
            "limits": {
                "min_free_ram_mb": min_ram,
                "worker_ram_estimate_mb": worker_ram,
                "max_cpu_pct": cpu_cap,
                "max_load_ratio": load_cap,
                "min_free_disk_gb": disk_floor,
            },
            "memory": dict(memory),
            "cpu": dict(cpu),
            "disk": dict(disk),
        }

    # 3. Compute RAM-bounded additional slots
    if effective_avail_ram is not None:
        ram_slots = max(0, (effective_avail_ram - min_ram) // worker_ram)
    else:
        # Telemetry unavailable: conservative fallback
        ram_slots = max(0, 2 - active_leases)

    # 4. Compute CPU / Load-bounded additional slots with hysteresis
    cpu_saturated = False
    if isinstance(cpu_busy, (int, float)) and math.isfinite(cpu_busy):
        if cpu_busy >= cpu_cap:
            cpu_saturated = True
            cpu_slots = 0
        else:
            headroom_pct = max(0.0, cpu_cap - float(cpu_busy))
            cpu_slots = max(1, int(math.ceil(cpu_count * (headroom_pct / 100.0))))
    elif isinstance(load_ratio, (int, float)) and math.isfinite(load_ratio):
        if load_ratio >= load_cap:
            cpu_saturated = True
            cpu_slots = 0
        else:
            headroom_ratio = max(0.0, load_cap - float(load_ratio))
            cpu_slots = max(1, int(math.ceil(cpu_count * headroom_ratio)))
    else:
        cpu_slots = max(1, cpu_count // 2)

    # When zero Summon leases are active and RAM + Disk floors are healthy, permit
    # 1 bootstrap slot even if background OS CPU load is high, avoiding starvation.
    if cpu_saturated and active_leases == 0 and ram_slots >= 1:
        cpu_slots = 1
        cpu_saturated = False

    remaining_cap = max(0, max_workers - active_leases)
    raw_additional = min(ram_slots, cpu_slots, remaining_cap)
    # Bounded step-up: admit at most max(2, cpu_count // 2) new workers per evaluation tick
    step_cap = max(2, cpu_count // 2)
    additional_slots = max(0, min(raw_additional, step_cap))
    recommended_total = min(max_workers, active_leases + additional_slots)

    floor_blocked = None
    reason = "resources_healthy"
    if additional_slots == 0:
        if remaining_cap == 0:
            floor_blocked = "max_workers_cap"
            reason = f"active leases ({active_leases}) reached max_auto_workers ({max_workers})"
        elif cpu_saturated:
            floor_blocked = "cpu_ceiling"
            reason = "host CPU / load exceeds ceiling while workers are active"
        elif ram_slots == 0:
            floor_blocked = "ram_floor"
            reason = "no additional RAM slots available above safety floor"
        else:
            floor_blocked = "step_cap"
            reason = "per-tick worker step-up limit reached"

    return {
        "schema": SCHEMA,
        "admitted": additional_slots > 0,
        "pressure": "healthy" if additional_slots > 0 else (floor_blocked or "throttled"),
        "floor_blocked": floor_blocked,
        "reason": reason,
        "active_leases": active_leases,
        "pending_reserved_ram_mb": pending_ram,
        "effective_avail_ram_mb": effective_avail_ram,
        "additional_slots": additional_slots,
        "recommended_total_slots": recommended_total,
        "max_auto_workers": max_workers,
        "limits": {
            "min_free_ram_mb": min_ram,
            "worker_ram_estimate_mb": worker_ram,
            "max_cpu_pct": cpu_cap,
            "max_load_ratio": load_cap,
            "min_free_disk_gb": disk_floor,
        },
        "memory": dict(memory),
        "cpu": dict(cpu),
        "disk": dict(disk),
    }


def snapshot_host_resources(
    cwd: str | None = None,
    job_root: str | None = None,
    *,
    sample_delay_sec: float = 0.02,
    min_free_ram_mb: int | None = None,
    worker_ram_estimate_mb: int | None = None,
    max_cpu_pct: float | None = None,
    max_load_ratio: float | None = None,
    min_free_disk_gb: float | None = None,
    max_auto_workers: int | None = None,
) -> dict[str, Any]:
    """Probe live host telemetry, reconcile leases, and return a `summon.resource-governor/v1` snapshot."""
    mem = probe_memory()
    cpu = probe_cpu(sample_delay_sec=sample_delay_sec)
    dsk = probe_disk(cwd=cwd, job_root=job_root)
    leases = reconcile_leases(job_root=job_root)
    return evaluate_admission(
        memory=mem,
        cpu=cpu,
        disk=dsk,
        lease_summary=leases,
        min_free_ram_mb=min_free_ram_mb,
        worker_ram_estimate_mb=worker_ram_estimate_mb,
        max_cpu_pct=max_cpu_pct,
        max_load_ratio=max_load_ratio,
        min_free_disk_gb=min_free_disk_gb,
        max_auto_workers=max_auto_workers,
    )


class ResourceGovernor:
    """Atomic host slot reservation manager for manifests and missions."""

    def __init__(
        self,
        *,
        cwd: str | None = None,
        job_root: str | None = None,
        min_free_ram_mb: int | None = None,
        worker_ram_estimate_mb: int | None = None,
        max_cpu_pct: float | None = None,
        max_load_ratio: float | None = None,
        min_free_disk_gb: float | None = None,
        max_auto_workers: int | None = None,
        probe_fn: Callable[[], tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] | None = None,
    ) -> None:
        self.cwd = os.path.abspath(cwd or os.getcwd())
        self.job_root = _jobs.resolve_jobs_dir(job_root)
        self.min_free_ram_mb = min_free_ram_mb
        self.worker_ram_estimate_mb = worker_ram_estimate_mb
        self.max_cpu_pct = max_cpu_pct
        self.max_load_ratio = max_load_ratio
        self.min_free_disk_gb = min_free_disk_gb
        self.max_auto_workers = max_auto_workers
        self._probe_fn = probe_fn

    def _probe(self) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        if self._probe_fn is not None:
            return self._probe_fn()
        return (
            probe_memory(),
            probe_cpu(sample_delay_sec=0.02),
            probe_disk(cwd=self.cwd, job_root=self.job_root),
        )

    def snapshot(self) -> dict[str, Any]:
        mem, cpu, dsk = self._probe()
        leases = reconcile_leases(job_root=self.job_root)
        return evaluate_admission(
            memory=mem,
            cpu=cpu,
            disk=dsk,
            lease_summary=leases,
            min_free_ram_mb=self.min_free_ram_mb,
            worker_ram_estimate_mb=self.worker_ram_estimate_mb,
            max_cpu_pct=self.max_cpu_pct,
            max_load_ratio=self.max_load_ratio,
            min_free_disk_gb=self.min_free_disk_gb,
            max_auto_workers=self.max_auto_workers,
        )

    def try_acquire_lease(
        self,
        *,
        backend: str = "default",
        job_id: str | None = None,
        ttl_sec: float = DEFAULT_LEASE_TTL_SEC,
    ) -> tuple[str | None, dict[str, Any]]:
        """Atomically evaluate host capacity under the registry lock and reserve 1 slot if admitted."""
        mem, cpu, dsk = self._probe()
        now = time.time()
        reg_path, lock_path, key_file = _registry_paths(self.job_root)
        with _registry_lock(lock_path):
            secret = _load_or_create_secret(key_file)
            reg = _read_registry_unlocked(reg_path, secret)
            raw_leases = reg.get("leases") or {}
            live_leases: dict[str, dict[str, Any]] = {}
            for lid, item in raw_leases.items():
                if _LEASE_ID_RE.fullmatch(str(lid)) and _is_lease_live(item, now):
                    live_leases[str(lid)] = dict(item)
            summary = _summarize_leases(live_leases, now)
            decision = evaluate_admission(
                memory=mem,
                cpu=cpu,
                disk=dsk,
                lease_summary=summary,
                min_free_ram_mb=self.min_free_ram_mb,
                worker_ram_estimate_mb=self.worker_ram_estimate_mb,
                max_cpu_pct=self.max_cpu_pct,
                max_load_ratio=self.max_load_ratio,
                min_free_disk_gb=self.min_free_disk_gb,
                max_auto_workers=self.max_auto_workers,
            )
            if not decision["admitted"] or len(live_leases) >= MAX_LEASES:
                if len(live_leases) != len(raw_leases):
                    reg["leases"] = live_leases
                    _write_registry_unlocked(reg_path, reg, secret)
                return None, decision

            lease_id = uuid.uuid4().hex
            pid = os.getpid()
            ram_est = int(decision["limits"]["worker_ram_estimate_mb"])
            live_leases[lease_id] = {
                "lease_id": lease_id,
                "owner_pid": pid,
                "owner_birth": process_birth_token(pid),
                "backend": str(backend or "default"),
                "job_id": str(job_id) if job_id else None,
                "state": "reserved",
                "reserved_ram_mb": ram_est,
                "created_at": now,
                "updated_at": now,
                "expires_at": now + max(10.0, float(ttl_sec)),
            }
            reg["leases"] = live_leases
            _write_registry_unlocked(reg_path, reg, secret)
            return lease_id, decision

    def renew_lease(self, lease_id: str | None, *, ttl_sec: float = DEFAULT_LEASE_TTL_SEC) -> bool:
        """Extend ``expires_at`` on an active lease held by the current process."""
        if not lease_id or not _LEASE_ID_RE.fullmatch(str(lease_id)):
            return False
        now = time.time()
        reg_path, lock_path, key_file = _registry_paths(self.job_root)
        with _registry_lock(lock_path):
            secret = _load_or_create_secret(key_file)
            reg = _read_registry_unlocked(reg_path, secret)
            leases = reg.get("leases") or {}
            item = leases.get(lease_id)
            if not isinstance(item, dict):
                return False
            item["updated_at"] = now
            item["expires_at"] = now + max(10.0, float(ttl_sec))
            leases[lease_id] = item
            reg["leases"] = leases
            _write_registry_unlocked(reg_path, reg, secret)
            return True

    def mark_lease_state(self, lease_id: str, state: str, *, owner_pid: int | None = None) -> bool:
        """Transition an active lease between 'reserved', 'launching', and 'running'."""
        if not _LEASE_ID_RE.fullmatch(str(lease_id)) or state not in ("reserved", "launching", "running"):
            return False
        now = time.time()
        reg_path, lock_path, key_file = _registry_paths(self.job_root)
        with _registry_lock(lock_path):
            secret = _load_or_create_secret(key_file)
            reg = _read_registry_unlocked(reg_path, secret)
            leases = reg.get("leases") or {}
            item = leases.get(lease_id)
            if not isinstance(item, dict):
                return False
            item["state"] = state
            item["updated_at"] = now
            item["expires_at"] = max(float(item.get("expires_at") or 0.0), now + DEFAULT_LEASE_TTL_SEC)
            if owner_pid is not None and isinstance(owner_pid, int) and owner_pid > 0:
                item["owner_pid"] = owner_pid
                item["owner_birth"] = process_birth_token(owner_pid)
            leases[lease_id] = item
            reg["leases"] = leases
            _write_registry_unlocked(reg_path, reg, secret)
            return True

    def release_lease(self, lease_id: str | None) -> bool:
        """Release a previously acquired host resource lease."""
        if not lease_id or not _LEASE_ID_RE.fullmatch(str(lease_id)):
            return False
        reg_path, lock_path, key_file = _registry_paths(self.job_root)
        with _registry_lock(lock_path):
            secret = _load_or_create_secret(key_file)
            reg = _read_registry_unlocked(reg_path, secret)
            leases = reg.get("leases") or {}
            if lease_id not in leases:
                return False
            leases.pop(lease_id, None)
            reg["leases"] = leases
            _write_registry_unlocked(reg_path, reg, secret)
            return True

    @contextmanager
    def acquire_slot(
        self,
        *,
        backend: str = "default",
        job_id: str | None = None,
        wait_timeout_sec: float | None = None,
        poll_interval_sec: float = 0.5,
        ttl_sec: float = DEFAULT_LEASE_TTL_SEC,
    ):
        """Acquire a host resource slot, waiting up to ``wait_timeout_sec`` when throttled.

        If ``active_leases == 0`` and a hard RAM or disk floor blocks admission past
        ``wait_timeout_sec``, raises ``ResourceExhaustedError``. Automatically renews
        the lease while the ``with`` block is active so long-horizon runs do not expire.
        """
        timeout = (
            wait_timeout_sec
            if wait_timeout_sec is not None
            else _env_float(
                "SUMMON_RESOURCE_WAIT_TIMEOUT_SEC",
                DEFAULT_RESOURCE_WAIT_TIMEOUT_SEC,
                0.0,
                86_400.0,
            )
        )
        deadline = time.monotonic() + timeout
        lease_id: str | None = None
        last_decision: dict[str, Any] = {}
        while True:
            lease_id, last_decision = self.try_acquire_lease(
                backend=backend, job_id=job_id, ttl_sec=ttl_sec
            )
            if lease_id is not None:
                break
            if time.monotonic() >= deadline:
                raise ResourceExhaustedError(
                    str(last_decision.get("reason") or "host resources exhausted"),
                    last_decision,
                )
            time.sleep(min(poll_interval_sec, max(0.05, deadline - time.monotonic())))
        stop_event = threading.Event()
        renew_interval = max(1.0, min(300.0, float(ttl_sec) / 3.0))

        def _renew_loop() -> None:
            while not stop_event.wait(renew_interval):
                try:
                    self.renew_lease(lease_id, ttl_sec=ttl_sec)
                except Exception:  # noqa: BLE001
                    pass

        renew_thread = threading.Thread(
            target=_renew_loop, name=f"summon-lease-renew-{lease_id[:8]}", daemon=True
        )
        renew_thread.start()
        try:
            yield lease_id, last_decision
        finally:
            stop_event.set()
            self.release_lease(lease_id)
