"""Windows Job Objects: kill a process tree independently of the leader's lifetime.

WHY THIS EXISTS (issue #10). ``taskkill /F /T /PID <leader>`` walks parent->child PID links,
so it can only find descendants WHILE THE LEADER IS ALIVE. Once the leader (the
``run_subagent.py`` subprocess) has exited, a still-running backend grandchild -- the one
holding the stdout pipe open and defeating the wall-clock timeout -- is orphaned and
survives the kill. POSIX never had this problem: a process GROUP outlives its leader, so
``killpg`` reaches the whole group through a dead leader.

A Job Object is the Windows equivalent of that guarantee. Processes are assigned to a
kernel object, descendants inherit it, and ``TerminateJobObject`` kills every member no
matter who has already exited.

``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` adds a second guarantee worth as much: if summon
itself dies -- crash, Ctrl-C, killed terminal -- the OS closes our handle and takes the
whole backend tree with it, instead of leaving a paid CLI running unattended.

KNOWN RESIDUAL RACE, stated plainly. Stock ``subprocess.Popen`` discards the child's thread
handle, so the textbook ``CREATE_SUSPENDED`` -> assign -> ``ResumeThread`` sequence is not
reachable with the stdlib. The assignment therefore happens immediately AFTER the child
starts. A grandchild spawned in the window between ``CreateProcess`` returning and
``AssignProcessToJobObject`` would escape the job. In practice that window is microseconds
against a child that spends milliseconds starting a Python or Node runtime before it spawns
anything, and the taskkill fallback still covers the leader-alive case. It is a strictly
smaller gap than the one it replaces, not a closed one.

Everything here fails soft: a machine or account where Job Objects are unavailable (or a
process already in a job that forbids nesting) simply keeps the old taskkill behaviour.
"""
from __future__ import annotations

import os

_AVAILABLE = os.name == "nt"

if _AVAILABLE:  # pragma: no cover - the import itself is platform-gated
    try:
        import ctypes
        from ctypes import wintypes

        _k32 = ctypes.WinDLL("kernel32", use_last_error=True)

        _JobObjectExtendedLimitInformation = 9
        _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

        class _IO_COUNTERS(ctypes.Structure):
            _fields_ = [("ReadOperationCount", ctypes.c_ulonglong),
                        ("WriteOperationCount", ctypes.c_ulonglong),
                        ("OtherOperationCount", ctypes.c_ulonglong),
                        ("ReadTransferCount", ctypes.c_ulonglong),
                        ("WriteTransferCount", ctypes.c_ulonglong),
                        ("OtherTransferCount", ctypes.c_ulonglong)]

        class _BASIC_LIMIT(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
                        ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
                        ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class _EXTENDED_LIMIT(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", _BASIC_LIMIT),
                        ("IoInfo", _IO_COUNTERS),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        _k32.CreateJobObjectW.restype = wintypes.HANDLE
        _k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        _k32.SetInformationJobObject.restype = wintypes.BOOL
        _k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                 wintypes.LPVOID, wintypes.DWORD]
        _k32.AssignProcessToJobObject.restype = wintypes.BOOL
        _k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        _k32.TerminateJobObject.restype = wintypes.BOOL
        _k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        _k32.CloseHandle.restype = wintypes.BOOL
        _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    except Exception:  # noqa: BLE001 - any ctypes/platform surprise disables the feature
        _AVAILABLE = False


_ATTR = "_summon_job_handle"


def available() -> bool:
    """True when Job Objects can be used on this platform/build."""
    return _AVAILABLE


def attach(process, *, detached: bool = False) -> bool:
    """Put a freshly spawned child (and its future descendants) in a kill-on-close job.

    Returns True when the child is in a job. Fails soft to False -- the caller keeps its
    existing taskkill path, which is correct whenever the leader is still alive.

    `detached=True` is a deliberate NO-OP: a `--background` child is supposed to outlive
    this process, and KILL_ON_JOB_CLOSE would execute it the moment summon exits.
    """
    if not _AVAILABLE or detached:
        return False
    handle = getattr(process, "_handle", None)
    if not handle:
        return False
    job = None
    try:
        job = _k32.CreateJobObjectW(None, None)
        if not job:
            return False
        info = _EXTENDED_LIMIT()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not _k32.SetInformationJobObject(
                job, _JobObjectExtendedLimitInformation,
                ctypes.byref(info), ctypes.sizeof(info)):
            _k32.CloseHandle(job)
            return False
        if not _k32.AssignProcessToJobObject(job, ctypes.c_void_p(int(handle))):
            # Most likely: this process is already in a job that forbids nesting (some CI
            # containers, older Windows). Not fatal -- taskkill still handles the common case.
            _k32.CloseHandle(job)
            return False
        setattr(process, _ATTR, job)
        return True
    except Exception:  # noqa: BLE001 - never let teardown plumbing break a dispatch
        if job:
            try:
                _k32.CloseHandle(job)
            except Exception:  # noqa: BLE001
                pass
        return False


def terminate(process) -> bool:
    """Kill every process in this child's job, whether or not the leader still exists.

    Returns True when the job handled it, so the caller can skip taskkill entirely.
    """
    job = getattr(process, _ATTR, None)
    if not _AVAILABLE or not job:
        return False
    try:
        _k32.TerminateJobObject(job, 1)
        return True
    except Exception:  # noqa: BLE001
        return False
    finally:
        close(process)


def close(process) -> None:
    """Release the job handle once the child is finished with.

    Held open for the child's whole lifetime ON PURPOSE: KILL_ON_JOB_CLOSE means the OS
    tears the tree down if summon dies unexpectedly. Closing it early would give that up.
    """
    job = getattr(process, _ATTR, None)
    if not job:
        return
    try:
        _k32.CloseHandle(job)
    except Exception:  # noqa: BLE001
        pass
    try:
        setattr(process, _ATTR, None)
    except Exception:  # noqa: BLE001
        pass
