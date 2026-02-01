"""全局配置管理"""

import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Config:
    """SatContainer 全局配置"""

    # 检查点相关
    checkpoint_ready_file: str = "/tmp/checkpoint_ready"
    checkpoint_enabled_env: str = "CHECKPOINT_ENABLED"

    # 环境变量名称
    original_entrypoint_env: str = "ORIGINAL_ENTRYPOINT"
    original_cmd_env: str = "ORIGINAL_CMD"

    # Wrapper相关
    wrapper_install_path: str = "/opt/satcontainer/checkpoint_wrapper.py"
    wrapper_log_prefix: str = "[SatContainer]"

    # 镜像标签
    injected_label: str = "satcontainer.injected"
    version_label: str = "satcontainer.version"

    # 默认超时（秒）
    import_timeout: int = 300

    @classmethod
    def from_env(cls) -> "Config":
        """从环境变量加载配置"""
        return cls(
            checkpoint_ready_file=os.environ.get(
                "SATCONTAINER_READY_FILE", cls.checkpoint_ready_file
            ),
            import_timeout=int(
                os.environ.get("SATCONTAINER_IMPORT_TIMEOUT", cls.import_timeout)
            ),
        )


# 全局默认配置实例
default_config = Config()
