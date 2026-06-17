#!/usr/bin/env python3

from __future__ import annotations

import ast
import importlib
import importlib.util
import json
import os
import runpy
import signal
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

_TAG  = "[satecode]"
_CFG  = "/etc/satcontainer"
_FILE = "run.json"

_SKIP = {
    "ast","os","sys","re","io","abc","copy","math","json","time","enum",
    "typing","types","functools","itertools","collections","contextlib",
    "pathlib","subprocess","threading","multiprocessing","logging",
    "argparse","struct","hashlib","hmac","base64","urllib","http",
    "socket","ssl","email","html","xml","csv","sqlite3","unittest",
    "traceback","warnings","weakref","gc","inspect","importlib",
    "pkgutil","platform","signal","shutil","tempfile","glob","fnmatch",
    "stat","errno","ctypes","dataclasses","operator","string","textwrap",
    "pprint","decimal","fractions","random","bisect","heapq","queue",
    "array","codecs","_thread","builtins","site","sysconfig","runpy",
    "zipimport","zipfile","tarfile","gzip",
}

_PY_FLAGS = {"-u","-B","-O","-OO","-s","-S","-E","-v","-W","-X","-c"}


def _ts() -> str:
    return time.strftime("%H:%M:%S")


def _log(msg: str) -> None:
    print("{} {} {}".format(_ts(), _TAG, msg), flush=True)


def _err(msg: str) -> None:
    print("{} {} ERR: {}".format(_ts(), _TAG, msg), file=sys.stderr, flush=True)


def _top_imports(src: str) -> Set[str]:
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return set()
    out: Set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                out.add(a.name.split(".")[0])
        elif isinstance(n, ast.ImportFrom):
            if n.module and n.level == 0:
                out.add(n.module.split(".")[0])
    return out


def _src_of(name: str) -> Optional[str]:
    try:
        spec = importlib.util.find_spec(name)
    except (ModuleNotFoundError, ValueError):
        return None
    if not spec or not spec.origin or not spec.origin.endswith(".py"):
        return None
    try:
        return Path(spec.origin).read_text(errors="replace")
    except OSError:
        return None


def _dep_graph(entry_src: str, depth: int = 3) -> List[str]:
    visited: Set[str] = set()
    order:   List[str] = []
    queue:   List[Tuple[str, int]] = [
        (n, 0) for n in _top_imports(entry_src) if n not in _SKIP
    ]
    head = 0
    while head < len(queue):
        mod, d = queue[head]; head += 1
        if mod in visited or mod in sys.modules or mod in _SKIP:
            continue
        visited.add(mod)
        order.append(mod)
        if d < depth:
            src = _src_of(mod)
            if src:
                for child in _top_imports(src):
                    if child not in visited and child not in _SKIP:
                        queue.append((child, d + 1))
    return order


def _preload(mods: List[str]) -> None:
    t0 = time.time()
    for i, name in enumerate(mods, 1):
        if name in sys.modules:
            continue
        t1 = time.time()
        try:
            importlib.import_module(name)
            _log("[{}/{}] {} ({:.2f}s)".format(i, len(mods), name, time.time() - t1))
        except ImportError as e:
            _log("[{}/{}] {} skip: {}".format(i, len(mods), name, e))
        except Exception as e:
            _err("{}: {}".format(name, e))
    _log("ready in {:.1f}s".format(time.time() - t0))


def _gate(token: str) -> None:
    try:
        Path(token).write_text(str(os.getpid()))
    except OSError as e:
        _err("token write: {}".format(e))
    sys.stdout.flush(); sys.stderr.flush()
    done = False

    def _h(s, f):
        nonlocal done; done = True

    old = signal.signal(signal.SIGUSR1, _h)
    _log("suspended")
    while not done:
        signal.pause()
    signal.signal(signal.SIGUSR1, old)
    _log("resumed")
    try:
        Path(token).unlink(missing_ok=True)
    except OSError:
        pass


def _load_cfg() -> Dict:
    d = os.environ.get("SATCONTAINER_CONFIG_DIR", _CFG)
    p = os.path.join(d, _FILE)
    if not os.path.isfile(p):
        return {}
    try:
        with open(p) as f:
            cfg = json.load(f)
        return cfg
    except Exception as e:
        _err("cfg: {}".format(e))
        return {}


def _apply_cfg(cfg: Dict) -> None:
    for k, v in (cfg.get("env") or {}).items():
        os.environ[k] = str(v)
    wd = cfg.get("workdir")
    if wd and os.path.isdir(wd):
        os.chdir(wd)


def _find_script(args: List[str]) -> Tuple[Optional[str], List[str], Optional[str]]:
    i = 0
    while i < len(args):
        t = args[i]
        if "python" in t or t.startswith("/usr") or t.startswith("/bin"):
            i += 1; continue
        if t in _PY_FLAGS:
            i += 1; continue
        if t in ("-X", "-W"):
            i += 2; continue
        if t == "-c":
            return None, args[i+1:], None
        if t == "-m":
            if i+1 >= len(args):
                return None, [], None
            mod = args[i+1]
            try:
                spec = importlib.util.find_spec(mod)
                if spec and spec.origin and spec.origin.endswith(".py"):
                    return spec.origin, args[i+2:], mod
            except Exception:
                pass
            return None, args[i+2:], None
        if t.endswith(".py"):
            return t, args[i+1:], None
        if os.path.isfile(t):
            try:
                if "python" in open(t).readline():
                    return t, args[i+1:], None
            except OSError:
                pass
        i += 1
    return None, [], None


def _run(script: str, argv: List[str], mod: Optional[str]) -> None:
    sys.argv = [script] + argv
    try:
        if mod:
            runpy.run_module(mod, run_name="__main__", alter_sys=True)
        else:
            runpy.run_path(script, run_name="__main__")
    except SystemExit as e:
        sys.exit(e.code if e.code is not None else 0)


def main() -> None:
    extra = sys.argv[1:]
    ep    = json.loads(os.environ.get("ORIGINAL_ENTRYPOINT", "[]"))
    cmd   = json.loads(os.environ.get("ORIGINAL_CMD",        "[]"))
    token = os.environ.get("CHECKPOINT_READY_FILE", "/tmp/checkpoint_ready")
    sync  = os.environ.get("CHECKPOINT_ENABLED", "") == "1"

    cfg = _load_cfg()
    _apply_cfg(cfg)
    args = cfg.get("args") or extra or cmd

    script, s_argv, mod = _find_script(ep + args)

    if script:
        sd = str(Path(script).parent.resolve())
        if sd not in sys.path:
            sys.path.insert(0, sd)
        try:
            src = Path(script).read_text(errors="replace")
        except OSError:
            src = ""
        mods = _dep_graph(src)
        _preload(mods)

    if sync:
        _gate(token)
        cfg  = _load_cfg()
        _apply_cfg(cfg)
        args = cfg.get("args") or extra or cmd
        script, s_argv, mod = _find_script(ep + args)

    if script and os.path.isfile(script):
        _run(script, s_argv or [], mod)
    else:
        full = ep + args
        if not full:
            _err("nothing to execute"); sys.exit(1)
        os.execvp(full[0], full)


# ---------------------------------------------------------------------------
# compat shim for tests
# ---------------------------------------------------------------------------

class CheckpointWrapper:

    def __init__(self):
        self.original_entrypoint = json.loads(os.environ.get("ORIGINAL_ENTRYPOINT", "[]"))
        self.original_cmd        = json.loads(os.environ.get("ORIGINAL_CMD", "[]"))
        self.checkpoint_enabled  = os.environ.get("CHECKPOINT_ENABLED", "") == "1"
        self.ready_file          = os.environ.get("CHECKPOINT_READY_FILE", "/tmp/checkpoint_ready")

    def find_python_script(self, args):
        return _find_script(args)

    def analyze_imports(self, path: str) -> List[str]:
        try:
            src  = Path(path).read_text(errors="replace")
            tree = ast.parse(src)
        except (OSError, SyntaxError):
            return []
        names: Set[str] = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                for a in n.names:
                    names.add(a.name)
            elif isinstance(n, ast.ImportFrom):
                if n.module and n.level == 0:
                    names.add(n.module)
        return sorted(names)

    def preload_modules(self, mods):
        _preload(mods)

    def run(self, extra=None):
        if extra:
            sys.argv = sys.argv[:1] + extra
        main()


if __name__ == "__main__":
    main()
