import ctypes
import ctypes.util
import errno
import hashlib
import os
import re
import select
import signal
import struct
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple


_INOTIFY_HDR = struct.Struct("iIII")
_IN_MASK = 0x00000108   # IN_CREATE | IN_CLOSE_WRITE


def _inotify_wait(directory: str, filename: str, timeout: float) -> bool:
    lib = ctypes.util.find_library("c")
    if not lib:
        return _poll_wait(directory, filename, timeout)
    libc = ctypes.CDLL(lib, use_errno=True)
    ifd = libc.inotify_init1(os.O_CLOEXEC | os.O_NONBLOCK)
    if ifd < 0:
        return _poll_wait(directory, filename, timeout)
    wd = libc.inotify_add_watch(ifd, directory.encode(), ctypes.c_uint32(_IN_MASK))
    if wd < 0:
        os.close(ifd)
        return _poll_wait(directory, filename, timeout)
    try:
        target   = filename.encode()
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return False
            r, _, _ = select.select([ifd], [], [], min(left, 1.0))
            if not r:
                continue
            try:
                buf = os.read(ifd, 4096)
            except OSError as e:
                if e.errno == errno.EAGAIN:
                    continue
                raise
            off = 0
            while off + _INOTIFY_HDR.size <= len(buf):
                _, _, _, nlen = _INOTIFY_HDR.unpack_from(buf, off)
                off += _INOTIFY_HDR.size
                name = b""
                if nlen:
                    name = buf[off:off + nlen].rstrip(b"\x00")
                    off += nlen
                if name == target:
                    return True
    finally:
        os.close(ifd)


def _poll_wait(directory: str, filename: str, timeout: float) -> bool:
    target   = os.path.join(directory, filename)
    deadline = time.monotonic() + timeout
    interval = 0.05
    while time.monotonic() < deadline:
        if os.path.exists(target):
            return True
        time.sleep(min(interval, deadline - time.monotonic()))
        interval = min(interval * 1.5, 2.0)
    return os.path.exists(target)


def _wait_token(token_path: str, timeout: float) -> bool:
    if os.path.exists(token_path):
        return True
    d = str(Path(token_path).parent)
    n = os.path.basename(token_path)
    try:
        return _inotify_wait(d, n, timeout)
    except Exception:
        return _poll_wait(d, n, timeout)


def _tree_digest(p: Path) -> str:
    h = hashlib.sha256()
    for fp in sorted(p.rglob("*")):
        if fp.is_file():
            h.update(fp.relative_to(p).as_posix().encode())
            with fp.open("rb") as fh:
                for chunk in iter(lambda: fh.read(65536), b""):
                    h.update(chunk)
    return h.hexdigest()


def _bundle_ok(p: Path) -> bool:
    return p.exists() and (
        (p / "config.json").exists()
        or (p / "images.tar").exists()
        or bool(list(p.glob("*.img")))
    )


def _deliver_signal(task_id: str, token_path: str, runtime: str, ns: str) -> None:
    pid = None
    try:
        pid = int(Path(token_path).read_text().strip())
    except (OSError, ValueError):
        pass
    if pid is not None:
        try:
            os.kill(pid, signal.SIGUSR1)
            return
        except (ProcessLookupError, PermissionError):
            pass
    for cmd in (
        ["docker", "kill", "-s", "SIGUSR1", task_id],
        [runtime, "-n", ns, "task", "kill", "--signal", "SIGUSR1", task_id],
    ):
        try:
            if subprocess.run(cmd, capture_output=True, timeout=10).returncode == 0:
                return
        except (subprocess.TimeoutExpired, FileNotFoundError):
            continue
    raise RuntimeError("cannot signal '{}'".format(task_id))


class SnapshotCoordinator:

    def __init__(self, task_id: str, output_dir,
                 token_path: str = "/tmp/checkpoint_ready",
                 runtime: str = "ctr", namespace: str = "default",
                 ready_timeout: float = 600.0, exec_timeout: float = 120.0):
        self.task_id       = task_id
        self.output_dir    = Path(output_dir)
        self.token_path    = token_path
        self.runtime       = runtime
        self.namespace     = namespace
        self.ready_timeout = ready_timeout
        self.exec_timeout  = exec_timeout

    def _acquire(self) -> Path:
        bundle = self.output_dir / "bundle"
        bundle.mkdir(parents=True, exist_ok=True)
        cmd = [self.runtime, "-n", self.namespace, "task", "checkpoint",
               "--checkpoint-path", str(bundle), self.task_id]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=self.exec_timeout)
        except subprocess.TimeoutExpired:
            raise RuntimeError("snapshot timed out after {}s".format(self.exec_timeout))
        if r.returncode != 0:
            raise RuntimeError("snapshot failed: {}".format(r.stderr.strip()))
        return bundle

    def run(self, resume: bool = True) -> dict:
        if not _wait_token(self.token_path, self.ready_timeout):
            raise TimeoutError("token not seen after {}s".format(self.ready_timeout))

        bundle = self._acquire()
        if not _bundle_ok(bundle):
            raise RuntimeError("incomplete bundle at {}".format(bundle))

        result = {
            "bundle_path": bundle,
            "digest":      _tree_digest(bundle),
            "task_id":     self.task_id,
            "timestamp":   time.time(),
        }
        if resume:
            _deliver_signal(self.task_id, self.token_path, self.runtime, self.namespace)
        return result


# ---------------------------------------------------------------------------
# Access sequence recorder (formerly "trace")
# ---------------------------------------------------------------------------

_OPENAT_RE = re.compile(r'^\d+\s+openat\(AT_FDCWD,\s*"([^"]+)",[^)]+\)\s*=\s*(\d+)')
_OPEN_RE   = re.compile(r'^\d+\s+open\("([^"]+)",[^)]+\)\s*=\s*(\d+)')
_EXEC_RE   = re.compile(r'^\d+\s+execve\("([^"]+)"')


def _parse_access_log(log_path: Path, rootfs: Path) -> List[str]:
    seen: Dict[str, int] = {}
    rank = 0
    try:
        lines = log_path.read_text(errors="replace").splitlines()
    except OSError:
        return []
    for line in lines:
        path = None
        for pat in (_OPENAT_RE, _OPEN_RE):
            m = pat.match(line)
            if m and int(m.group(2)) >= 0:
                path = m.group(1)
                break
        if path is None:
            m = _EXEC_RE.match(line)
            if m:
                path = m.group(1)
        if path is None:
            continue
        try:
            rel = str(Path(path).resolve().relative_to(rootfs))
        except (ValueError, OSError):
            continue
        if rel not in seen:
            seen[rel] = rank
            rank += 1
    return sorted(seen, key=lambda k: seen[k])


def _all_files(rootfs: Path) -> List[str]:
    out = []
    for p in sorted(rootfs.rglob("*")):
        if p.is_file() or p.is_symlink():
            out.append(str(p.relative_to(rootfs)))
    return out


def record_access_sequence(
    restore_cmd: List[str],
    rootfs: Path,
    strace_bin: str = "strace",
) -> Tuple[List[str], List[str]]:
    import tempfile as _tmp
    with _tmp.TemporaryDirectory(prefix="sec_trace_") as td:
        log = os.path.join(td, "acc.log")
        cmd = [strace_bin, "-f", "-e", "trace=open,openat,execve", "-o", log, "--"] + restore_cmd
        try:
            subprocess.run(cmd, timeout=300)
        except subprocess.TimeoutExpired:
            pass
        except FileNotFoundError:
            raise RuntimeError("strace not found: '{}'".format(strace_bin))
        tier_a = _parse_access_log(Path(log), rootfs)

    all_f  = _all_files(rootfs)
    s      = set(tier_a)
    tier_b = [f for f in all_f if f not in s]
    return tier_a, tier_b


def record_from_log(log_path: Path, rootfs: Path) -> Tuple[List[str], List[str]]:
    tier_a = _parse_access_log(log_path, rootfs)
    all_f  = _all_files(rootfs)
    s      = set(tier_a)
    tier_b = [f for f in all_f if f not in s]
    return tier_a, tier_b
