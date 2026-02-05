#!/usr/bin/env python3
"""
检查点Wrapper - 在容器内运行

自动分析原始入口脚本的import语句，导入依赖后发出检查点信号。
通过直接执行脚本（而非exec新进程）来保留预加载的模块。

启动参数优先级：
    1. 配置文件 /etc/satcontainer/run.json（用于 restore 时修改参数）
    2. wrapper 的命令行参数（正常启动时使用）

环境变量:
    CHECKPOINT_ENABLED: "1" 启用检查点模式（阻塞等待）
    ORIGINAL_ENTRYPOINT: JSON格式的原始入口点
    ORIGINAL_CMD: JSON格式的原始CMD
    CHECKPOINT_READY_FILE: ready标记文件路径（默认/tmp/checkpoint_ready）
    SATCONTAINER_CONFIG_DIR: 配置文件目录（默认/etc/satcontainer）
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import json
import os
import signal
import sys
import time
import runpy
from typing import Dict, List, Optional


LOG_PREFIX = "[SatContainer]"

# 默认配置目录和文件
DEFAULT_CONFIG_DIR = "/etc/satcontainer"
RUN_CONFIG_FILE = "run.json"


def log(msg):
    # type: (str) -> None
    """输出带前缀的日志"""
    print("{} {}".format(LOG_PREFIX, msg), flush=True)


def log_error(msg):
    # type: (str) -> None
    """输出错误日志"""
    print("{} ERROR: {}".format(LOG_PREFIX, msg), file=sys.stderr, flush=True)


class RunConfig:
    """
    运行配置 - 用于 restore 时修改启动参数

    配置文件格式 (/etc/satcontainer/run.json):
    {
        "args": ["arg1", "arg2", "--flag", "value"],  // 脚本参数（可选）
        "env": {                                       // 额外环境变量（可选）
            "KEY": "value"
        },
        "workdir": "/path/to/workdir"                  // 工作目录（可选）
    }
    """

    def __init__(self, config_dir=None):
        # type: (Optional[str]) -> None
        self.config_dir = config_dir or os.environ.get("SATCONTAINER_CONFIG_DIR", DEFAULT_CONFIG_DIR)
        self.config_file = os.path.join(self.config_dir, RUN_CONFIG_FILE)
        self._config = None

    def exists(self):
        # type: () -> bool
        """检查配置文件是否存在"""
        return os.path.isfile(self.config_file)

    def load(self):
        # type: () -> bool
        """加载配置文件，返回是否成功"""
        if not self.exists():
            return False

        try:
            with open(self.config_file, "r") as f:
                self._config = json.load(f)
            log("Loaded run config from: {}".format(self.config_file))
            return True
        except Exception as e:
            log_error("Failed to load config {}: {}".format(self.config_file, e))
            return False

    def get_args(self):
        # type: () -> Optional[List[str]]
        """获取启动参数，如果未配置返回 None"""
        if self._config is None:
            return None
        return self._config.get("args")

    def get_env(self):
        # type: () -> Optional[Dict[str, str]]
        """获取额外环境变量"""
        if self._config is None:
            return None
        return self._config.get("env")

    def get_workdir(self):
        # type: () -> Optional[str]
        """获取工作目录"""
        if self._config is None:
            return None
        return self._config.get("workdir")

    def apply_env(self):
        # type: () -> None
        """应用环境变量配置"""
        env = self.get_env()
        if env:
            for key, value in env.items():
                os.environ[key] = str(value)
                log("Set env: {}={}".format(key, value))

    def apply_workdir(self):
        # type: () -> None
        """应用工作目录配置"""
        workdir = self.get_workdir()
        if workdir and os.path.isdir(workdir):
            os.chdir(workdir)
            log("Changed workdir to: {}".format(workdir))


class CheckpointWrapper:
    """
    检查点wrapper - 在容器内运行

    自动分析原始入口脚本的import语句，导入依赖后发出检查点信号。
    通过在同一进程内执行脚本来保留预加载的模块。

    支持从配置文件读取启动参数，用于 restore 时修改参数。
    """

    def __init__(self):
        self.ready_file = os.environ.get("CHECKPOINT_READY_FILE", "/tmp/checkpoint_ready")
        self.checkpoint_enabled = os.environ.get("CHECKPOINT_ENABLED", "") == "1"

        # 解析原始入口点
        self.original_entrypoint = json.loads(
            os.environ.get("ORIGINAL_ENTRYPOINT", "[]")
        )
        self.original_cmd = json.loads(
            os.environ.get("ORIGINAL_CMD", "[]")
        )

        # 运行配置
        self.run_config = RunConfig()

    def find_python_script(self, args):
        # type: (List[str]) -> tuple
        """
        从命令行参数中找到Python脚本路径和脚本参数

        Returns:
            (script_path, script_args) 或 (None, None)
        """
        if not args:
            return None, None

        i = 0
        while i < len(args):
            arg = args[i]

            # 跳过python解释器
            if arg in ("python", "python3") or arg.startswith("python3.") or arg.startswith("/"):
                if "python" in arg:
                    i += 1
                    continue

            # 跳过python参数
            if arg in ("-u", "-B", "-O", "-OO", "-s", "-S", "-E", "-v"):
                i += 1
                continue

            # 带值的参数
            if arg in ("-X", "-W"):
                i += 2
                continue

            # -c 和 -m 不是脚本文件
            if arg in ("-c", "-m"):
                return None, None

            # 找到脚本文件
            if arg.endswith(".py"):
                return arg, args[i+1:]

            # 可能是可执行脚本
            if os.path.isfile(arg):
                try:
                    with open(arg, "r") as f:
                        first_line = f.readline()
                        if "python" in first_line:
                            return arg, args[i+1:]
                except Exception:
                    pass

            i += 1

        return None, None

    def analyze_imports(self, script_path):
        # type: (str) -> List[str]
        """
        使用AST分析Python脚本的import语句

        Returns:
            顶层模块名列表，如 ['torch', 'numpy', 'PIL']
        """
        try:
            with open(script_path, "r") as f:
                source = f.read()

            tree = ast.parse(source)
        except Exception as e:
            log_error("Failed to parse {}: {}".format(script_path, e))
            return []

        modules = set()

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top_module = alias.name.split(".")[0]
                    modules.add(top_module)

            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    top_module = node.module.split(".")[0]
                    modules.add(top_module)

        return sorted(modules)

    def preload_modules(self, modules):
        # type: (List[str]) -> Dict[str, float]
        """
        动态导入模块列表

        Returns:
            每个模块的加载时间（秒）
        """
        load_times = {}

        for module_name in modules:
            if module_name in sys.modules:
                log("Module '{}' already loaded, skipping".format(module_name))
                continue

            start_time = time.time()
            try:
                importlib.import_module(module_name)
                elapsed = time.time() - start_time
                load_times[module_name] = elapsed
                log("Loaded '{}' in {:.2f}s".format(module_name, elapsed))

            except ImportError as e:
                log("Failed to import '{}': {} (skipping)".format(module_name, e))
            except Exception as e:
                log_error("Error importing '{}': {}".format(module_name, e))

        return load_times

    def signal_ready_and_block(self):
        # type: () -> None
        """发出ready信号并阻塞等待SIGUSR1信号"""
        log("Creating ready file: {}".format(self.ready_file))
        try:
            with open(self.ready_file, "w") as f:
                f.write(str(os.getpid()))
        except Exception as e:
            log_error("Failed to create ready file: {}".format(e))

        sys.stdout.flush()
        sys.stderr.flush()

        # 使用信号等待方式阻塞，避免Docker自动恢复SIGSTOP
        # 外部发送 SIGUSR1 来恢复执行
        self._resumed = False

        def handle_resume(signum, frame):
            self._resumed = True

        # 注册信号处理器
        old_handler = signal.signal(signal.SIGUSR1, handle_resume)

        log("Waiting for SIGUSR1 signal to continue (send: docker kill -s SIGUSR1 <container>)...")

        # 阻塞等待信号
        while not self._resumed:
            signal.pause()

        # 恢复原来的信号处理器
        signal.signal(signal.SIGUSR1, old_handler)

        log("Received SIGUSR1, continuing to original program...")

        try:
            if os.path.exists(self.ready_file):
                os.remove(self.ready_file)
        except Exception:
            pass

    def get_effective_args(self, extra_args):
        # type: (List[str]) -> List[str]
        """
        获取有效的启动参数

        优先级：
        1. 配置文件中的 args
        2. wrapper 的命令行参数 (extra_args)
        3. 原始 CMD

        Returns:
            脚本参数列表
        """
        # 尝试从配置文件加载
        if self.run_config.load():
            config_args = self.run_config.get_args()
            if config_args is not None:
                log("Using args from config file: {}".format(config_args))
                # 应用环境变量和工作目录
                self.run_config.apply_env()
                self.run_config.apply_workdir()
                return config_args
            else:
                log("Config file exists but no 'args' specified, using default")
                # 仍然应用环境变量和工作目录
                self.run_config.apply_env()
                self.run_config.apply_workdir()

        # 使用传入的参数或默认 CMD
        if extra_args:
            log("Using wrapper args: {}".format(extra_args))
            return extra_args
        else:
            log("Using original CMD: {}".format(self.original_cmd))
            return self.original_cmd

    def run_script_in_process(self, script_path, script_args):
        # type: (str, List[str]) -> None
        """
        在当前进程内执行Python脚本，保留已加载的模块

        Args:
            script_path: 脚本路径
            script_args: 脚本参数
        """
        # 设置sys.argv为脚本期望的格式
        sys.argv = [script_path] + script_args

        log("Running script in-process: {} {}".format(script_path, " ".join(script_args)))

        # 使用runpy在当前进程中执行脚本
        # run_path会将脚本作为__main__模块执行
        try:
            runpy.run_path(script_path, run_name="__main__")
        except SystemExit as e:
            # 脚本正常退出
            sys.exit(e.code if e.code is not None else 0)

    def exec_original(self, script_args):
        # type: (List[str]) -> None
        """
        执行原始入口程序（用于非Python脚本或无法在进程内执行的情况）

        Args:
            script_args: 脚本参数
        """
        args = self.original_entrypoint + script_args

        if not args:
            log_error("No original entrypoint or command to execute")
            sys.exit(1)

        log("Executing (exec): {}".format(" ".join(args)))

        try:
            os.execvp(args[0], args)
        except FileNotFoundError:
            log_error("Command not found: {}".format(args[0]))
            sys.exit(127)
        except PermissionError:
            log_error("Permission denied: {}".format(args[0]))
            sys.exit(126)

    def run(self, extra_args):
        # type: (List[str]) -> None
        """
        主流程

        Args:
            extra_args: 运行时额外参数（docker run/ctr run 时传入）
        """
        log("Checkpoint wrapper starting...")
        log("Original ENTRYPOINT: {}".format(self.original_entrypoint))
        log("Original CMD: {}".format(self.original_cmd))
        log("Extra args: {}".format(extra_args))
        log("Checkpoint enabled: {}".format(self.checkpoint_enabled))
        log("Config file: {}".format(self.run_config.config_file))

        # 获取有效的脚本参数（优先从配置文件读取）
        effective_args = self.get_effective_args(extra_args)

        # 构建完整命令
        full_cmd = self.original_entrypoint + effective_args

        log("Full command: {}".format(full_cmd))

        # 查找Python脚本
        script_path, script_args = self.find_python_script(full_cmd)

        if script_path and os.path.isfile(script_path):
            log("Found Python script: {}".format(script_path))
            log("Script arguments: {}".format(script_args))

            # AST分析import语句
            log("Analyzing imports in {}...".format(script_path))
            modules = self.analyze_imports(script_path)

            if modules:
                log("Found {} modules to preload: {}".format(len(modules), modules))

                # 预加载模块
                start_time = time.time()
                load_times = self.preload_modules(modules)
                total_time = time.time() - start_time

                log("Preloaded {} modules in {:.2f}s".format(len(load_times), total_time))
            else:
                log("No third-party modules found to preload")

            # 如果启用了检查点模式，阻塞等待
            if self.checkpoint_enabled:
                log("Checkpoint mode enabled, blocking...")
                self.signal_ready_and_block()

                # 阻塞恢复后，重新读取配置文件（支持 restore 时修改参数）
                log("Re-checking config file after resume...")
                effective_args = self.get_effective_args(extra_args)
                full_cmd = self.original_entrypoint + effective_args
                script_path, script_args = self.find_python_script(full_cmd)
                log("Final command after resume: {}".format(full_cmd))

            # 在当前进程内执行脚本（保留预加载的模块）
            self.run_script_in_process(script_path, script_args or [])

        else:
            log("No Python script found or script not exists, using exec")

            # 如果启用了检查点模式，阻塞等待
            if self.checkpoint_enabled:
                log("Checkpoint mode enabled, blocking...")
                self.signal_ready_and_block()

                # 阻塞恢复后，重新读取配置文件
                log("Re-checking config file after resume...")
                effective_args = self.get_effective_args(extra_args)

            # 非Python脚本，使用exec
            self.exec_original(effective_args)


def main():
    """入口点"""
    extra_args = sys.argv[1:]

    wrapper = CheckpointWrapper()
    wrapper.run(extra_args)


if __name__ == "__main__":
    main()
