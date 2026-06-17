import os
from dataclasses import dataclass


@dataclass
class Config:
    sync_token_path: str = "/tmp/checkpoint_ready"
    sync_mode_env:   str = "CHECKPOINT_ENABLED"
    base_cmd_env:    str = "ORIGINAL_ENTRYPOINT"
    base_args_env:   str = "ORIGINAL_CMD"
    shim_path:       str = "/opt/satcontainer/checkpoint_wrapper.py"
    patched_label:   str = "satecode.patched"
    build_label:     str = "satecode.build"
    stage_timeout:   int = 300

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            sync_token_path=os.environ.get("SATCONTAINER_READY_FILE", cls.sync_token_path),
            stage_timeout=int(os.environ.get("SATCONTAINER_IMPORT_TIMEOUT", cls.stage_timeout)),
        )


default_config = Config()
