"""Pre-import audit fence for owned synthetic council processes only."""
from pathlib import Path
import os
import ctypes  # Initialize public stdlib support before denying runtime FFI lookups.
import subprocess
import sys


def install(packet, *, child=False):
    packet = Path(packet).resolve()
    scripts = packet / "scripts"
    public = tuple({Path(sys.base_prefix).resolve(), Path(sys.prefix).resolve()})
    denied = []
    descriptors = {}
    launch = {"command": None, "pids": set()}

    def refuse(event):
        denied.append(event)
        raise PermissionError("synthetic audit boundary refused: " + event)

    def inside(path, root):
        return path == root or root in path.parents

    def resolve(value, directory=None):
        if isinstance(value, int):
            if value in descriptors:
                return descriptors[value]
            return refuse("unowned_descriptor")
        path = Path(os.fsdecode(value))
        if not path.is_absolute() and directory not in (None, -1):
            if directory not in descriptors:
                return refuse("unowned_directory_descriptor")
            path = descriptors[directory] / path
        return path.resolve()

    def check(value, write=False, directory=None):
        path = resolve(value, directory)
        allowed = inside(path, packet) if write else any(inside(path, root) for root in (packet, *public))
        if not allowed or (write and inside(path, scripts)):
            refuse("file_write" if write else "file_read")
        return path

    def audit(event, args):
        if event == "open":
            value, mode, flags = args
            if isinstance(value, int):
                if value not in descriptors and value not in (0, 1, 2):
                    refuse("unowned_descriptor")
                return
            write = bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
            check(value, write)
        elif event in ("os.listdir", "os.scandir"):
            check(args[0] if args and args[0] is not None else os.getcwd())
        elif event in ("os.remove", "os.rmdir"):
            check(args[0], True, args[1] if len(args) > 1 else None)
        elif event in ("os.mkdir", "os.chmod", "os.utime"):
            check(args[0], True, args[-1] if len(args) > 2 and isinstance(args[-1], int) else None)
        elif event in ("os.rename", "os.link"):
            check(args[0], True, args[2] if len(args) > 2 else None)
            check(args[1], True, args[3] if len(args) > 3 else None)
        elif event in ("os.symlink", "os.truncate", "os.chown", "os.fchmod", "os.fchown"):
            refuse(event)
        elif event == "os.chdir":
            if not inside(resolve(args[0]), packet):
                refuse(event)
        elif event == "subprocess.Popen":
            expected = launch["command"]
            executable, command = args[:2]
            if child or expected is None or (executable is not None and Path(executable).resolve() != Path(sys.executable).resolve()):
                refuse(event)
            if command != expected and command != subprocess.list2cmdline(expected):
                refuse(event)
        elif event in ("os.kill", "os.killpg"):
            # Only the owned children this fence authorized may be signalled (their
            # process group id equals their pid under start_new_session).
            if child or args[0] not in launch["pids"]:
                refuse(event)
        elif event.startswith(("socket.", "os.exec", "os.spawn", "os.posix_spawn", "os.fork", "os.startfile")) or event in ("os.system", "pty.spawn"):
            refuse(event)
        elif event in ("ctypes.dlsym", "ctypes.dlsym/handle") or (event == "ctypes.dlopen" and args[0] is not None):
            refuse(event)
        elif event.startswith("winreg."):
            refuse(event)

    real_open, real_close = os.open, os.close
    def owned_open(path, flags, mode=0o777, *, dir_fd=None):
        resolved = resolve(path, dir_fd)
        fd = real_open(path, flags, mode, dir_fd=dir_fd)
        descriptors[fd] = resolved
        return fd
    def owned_close(fd):
        try:
            return real_close(fd)
        finally:
            descriptors.pop(fd, None)
    os.open, os.close = owned_open, owned_close
    sys.addaudithook(audit)
    if os.name != "nt":
        # POSIX counterpart of the CreatePipe/open_osfhandle wrap below: the only
        # pipes this process may create are the ones subprocess makes for the single
        # authorized launch, and they become owned descriptors.
        real_os_pipe = os.pipe

        def owned_pipe():
            if child or launch["command"] is None:
                refuse("os.pipe")
            read_fd, write_fd = real_os_pipe()
            descriptors[read_fd] = packet / "owned-child-pipe"
            descriptors[write_fd] = packet / "owned-child-pipe"
            return read_fd, write_fd

        os.pipe = owned_pipe
    if os.name == "nt":
        import _winapi
        import msvcrt
        real_pipe, real_fd = _winapi.CreatePipe, msvcrt.open_osfhandle
        pipe_handles = set()
        def create_pipe(*args):
            if child or launch["command"] is None:
                refuse("_winapi.CreatePipe")
            handles = real_pipe(*args)
            pipe_handles.update(handles)
            return handles
        def pipe_fd(handle, flags):
            if handle not in pipe_handles:
                refuse("msvcrt.open_osfhandle")
            fd = real_fd(handle, flags)
            descriptors[fd] = packet / "owned-child-pipe"
            pipe_handles.discard(handle)
            return fd
        _winapi.CreatePipe, msvcrt.open_osfhandle = create_pipe, pipe_fd
        real_create = _winapi.CreateProcess
        def create_process(application, command, *args):
            expected = launch["command"]
            if child or expected is None or command != subprocess.list2cmdline(expected):
                refuse("_winapi.CreateProcess")
            if application is not None and Path(application).resolve() != Path(sys.executable).resolve():
                refuse("_winapi.CreateProcess")
            return real_create(application, command, *args)
        _winapi.CreateProcess = create_process
    return denied, launch


