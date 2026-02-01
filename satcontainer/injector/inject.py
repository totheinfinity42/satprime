"""镜像注入逻辑实现 - 直接操作OCI格式tar文件"""

import gzip
import hashlib
import io
import json
import tarfile
import tempfile
from pathlib import Path
from typing import Optional

from satcontainer.config import default_config, Config


class ImageInjector:
    """镜像注入器 - 将wrapper程序注入到OCI格式的容器镜像tar文件中"""

    def __init__(
        self,
        input_tar: str,
        dockerfile: Optional[str] = None,
        config: Optional[Config] = None,
    ):
        """
        Args:
            input_tar: 输入OCI镜像tar文件路径（如 demo_apps/mmrotate/mmrotate.tar）
            dockerfile: 可选的Dockerfile路径（用于辅助分析入口点）
            config: 配置对象，默认使用全局配置
        """
        self.input_tar = Path(input_tar)
        self.dockerfile = Path(dockerfile) if dockerfile else None
        self.config = config or default_config

        if not self.input_tar.exists():
            raise FileNotFoundError(f"Input tar file not found: {self.input_tar}")

        self._manifest: Optional[list] = None
        self._image_config: Optional[dict] = None
        self._config_digest: Optional[str] = None

    def _load_manifest(self, tar: tarfile.TarFile) -> list:
        """加载manifest.json"""
        manifest_file = tar.extractfile("manifest.json")
        if manifest_file is None:
            raise ValueError("manifest.json not found in tar")
        return json.load(manifest_file)

    def _load_image_config(self, tar: tarfile.TarFile) -> dict:
        """加载镜像配置文件"""
        if self._manifest is None:
            self._manifest = self._load_manifest(tar)

        # manifest.json是一个数组，通常只有一个元素
        config_path = self._manifest[0]["Config"]
        self._config_digest = config_path

        config_file = tar.extractfile(config_path)
        if config_file is None:
            raise ValueError(f"Config file not found: {config_path}")
        return json.load(config_file)

    def get_original_entrypoint(self) -> tuple[list, list]:
        """返回原始的 (ENTRYPOINT, CMD)"""
        with tarfile.open(self.input_tar, "r") as tar:
            if self._image_config is None:
                self._image_config = self._load_image_config(tar)

        config = self._image_config.get("config", {})
        entrypoint = config.get("Entrypoint") or []
        cmd = config.get("Cmd") or []
        return entrypoint, cmd

    def is_injected(self) -> bool:
        """检查镜像是否已经被注入"""
        with tarfile.open(self.input_tar, "r") as tar:
            if self._image_config is None:
                self._image_config = self._load_image_config(tar)

        labels = self._image_config.get("config", {}).get("Labels") or {}
        return labels.get(self.config.injected_label) == "true"

    def _get_wrapper_content(self) -> bytes:
        """获取wrapper程序内容"""
        wrapper_path = Path(__file__).parent / "wrapper" / "checkpoint_wrapper.py"
        return wrapper_path.read_bytes()

    def _parse_dockerfile_entrypoint(self) -> tuple[Optional[list], Optional[list]]:
        """从Dockerfile解析ENTRYPOINT和CMD（作为备用）"""
        if self.dockerfile is None or not self.dockerfile.exists():
            return None, None

        entrypoint = None
        cmd = None

        content = self.dockerfile.read_text()
        for line in content.splitlines():
            line = line.strip()
            if line.upper().startswith("ENTRYPOINT"):
                # 解析 ENTRYPOINT ["python", "app.py"] 或 ENTRYPOINT python app.py
                entrypoint = self._parse_dockerfile_instruction(line, "ENTRYPOINT")
            elif line.upper().startswith("CMD"):
                cmd = self._parse_dockerfile_instruction(line, "CMD")

        return entrypoint, cmd

    def _parse_dockerfile_instruction(self, line: str, instruction: str) -> list:
        """解析Dockerfile指令"""
        # 移除指令前缀
        value = line[len(instruction):].strip()

        # JSON格式: ["python", "app.py"]
        if value.startswith("["):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                pass

        # Shell格式: python app.py
        return ["sh", "-c", value]

    def _create_layer_tar(self, files: dict[str, bytes]) -> bytes:
        """
        创建一个新的layer tar（未压缩）

        Args:
            files: {文件路径: 文件内容} 字典

        Returns:
            tar文件的bytes内容
        """
        tar_buffer = io.BytesIO()

        with tarfile.open(fileobj=tar_buffer, mode="w") as tar:
            for filepath, content in files.items():
                # 创建目录结构
                parts = Path(filepath).parts
                for i in range(1, len(parts)):
                    dir_path = "/".join(parts[:i])
                    try:
                        tar.getmember(dir_path)
                    except KeyError:
                        dir_info = tarfile.TarInfo(name=dir_path)
                        dir_info.type = tarfile.DIRTYPE
                        dir_info.mode = 0o755
                        tar.addfile(dir_info)

                # 添加文件
                info = tarfile.TarInfo(name=filepath.lstrip("/"))
                info.size = len(content)
                info.mode = 0o755 if filepath.endswith(".py") else 0o644
                tar.addfile(info, io.BytesIO(content))

        return tar_buffer.getvalue()

    def _calculate_digest(self, content: bytes) -> str:
        """计算sha256摘要"""
        return hashlib.sha256(content).hexdigest()

    def inject(
        self,
        output_tar: str,
        force: bool = False,
        tag_suffix: str = "-wrapped",
    ) -> str:
        """
        执行注入，返回输出tar文件路径

        注入内容：
        1. wrapper程序（作为新layer）
        2. 原始ENTRYPOINT/CMD保存到环境变量
        3. 替换ENTRYPOINT为wrapper

        Args:
            output_tar: 输出tar文件路径
            force: 是否强制覆盖已存在的文件
            tag_suffix: 镜像标签后缀（默认 "-wrapped"），设为 None 保持原名

        Returns:
            输出tar文件路径
        """
        output_path = Path(output_tar)

        if output_path.exists() and not force:
            raise FileExistsError(
                f"Output file '{output_tar}' already exists. Use --force to overwrite."
            )

        # 创建临时目录
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)

            # 解压原始tar
            with tarfile.open(self.input_tar, "r") as tar:
                tar.extractall(tmpdir)
                self._manifest = self._load_manifest(tar)
                self._image_config = self._load_image_config(tar)

            # 获取原始入口点
            config = self._image_config.get("config", {})
            original_entrypoint = config.get("Entrypoint") or []
            original_cmd = config.get("Cmd") or []

            # 如果镜像没有入口点，尝试从Dockerfile解析
            if not original_entrypoint and not original_cmd:
                df_entrypoint, df_cmd = self._parse_dockerfile_entrypoint()
                if df_entrypoint:
                    original_entrypoint = df_entrypoint
                if df_cmd:
                    original_cmd = df_cmd

            entrypoint_json = json.dumps(original_entrypoint)
            cmd_json = json.dumps(original_cmd)

            # 创建新layer（包含wrapper程序）
            wrapper_content = self._get_wrapper_content()
            wrapper_path = self.config.wrapper_install_path.lstrip("/")

            new_layer_tar = self._create_layer_tar({
                wrapper_path: wrapper_content,
            })

            # 计算未压缩layer的diff_id (sha256)
            new_layer_diff_id = f"sha256:{self._calculate_digest(new_layer_tar)}"

            # 压缩新layer
            new_layer_gz = gzip.compress(new_layer_tar)
            new_layer_digest = self._calculate_digest(new_layer_gz)
            new_layer_filename = f"{new_layer_digest}.tar.gz"

            # 写入新layer文件
            (tmpdir / new_layer_filename).write_bytes(new_layer_gz)

            # 更新镜像配置
            self._update_image_config(
                entrypoint_json=entrypoint_json,
                cmd_json=cmd_json,
                new_layer_diff_id=new_layer_diff_id,
            )

            # 写入新配置文件
            new_config_json = json.dumps(self._image_config, indent=2).encode()
            new_config_digest = self._calculate_digest(new_config_json)
            new_config_filename = f"{new_config_digest}.json"

            # 删除旧配置文件
            old_config_path = tmpdir / self._config_digest
            if old_config_path.exists():
                old_config_path.unlink()

            # 写入新配置文件
            (tmpdir / new_config_filename).write_bytes(new_config_json)

            # 更新manifest.json
            self._manifest[0]["Config"] = new_config_filename
            self._manifest[0]["Layers"].append(new_layer_filename)

            # 更新镜像标签
            if "RepoTags" in self._manifest[0] and self._manifest[0]["RepoTags"]:
                if tag_suffix:
                    # 给每个标签添加后缀
                    new_tags = []
                    for tag in self._manifest[0]["RepoTags"]:
                        if ":" in tag:
                            name, version = tag.rsplit(":", 1)
                            new_tags.append("{}{}:{}".format(name, tag_suffix, version))
                        else:
                            new_tags.append("{}{}".format(tag, tag_suffix))
                    self._manifest[0]["RepoTags"] = new_tags
                # 如果 tag_suffix 为 None，保持原标签不变
            else:
                self._manifest[0]["RepoTags"] = ["satcontainer-injected:latest"]

            (tmpdir / "manifest.json").write_text(
                json.dumps(self._manifest, indent=2)
            )

            # 打包新tar
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with tarfile.open(output_path, "w") as tar:
                for item in tmpdir.iterdir():
                    tar.add(item, arcname=item.name)

        return str(output_path)

    def _update_image_config(
        self,
        entrypoint_json: str,
        cmd_json: str,
        new_layer_diff_id: str,
    ) -> None:
        """更新镜像配置"""
        config = self._image_config.setdefault("config", {})

        # 更新环境变量
        env = config.setdefault("Env", [])

        # 移除已存在的相关环境变量
        env = [e for e in env if not e.startswith(f"{self.config.original_entrypoint_env}=")
               and not e.startswith(f"{self.config.original_cmd_env}=")]

        # 添加新的环境变量
        env.append(f"{self.config.original_entrypoint_env}={entrypoint_json}")
        env.append(f"{self.config.original_cmd_env}={cmd_json}")
        config["Env"] = env

        # 更新Labels
        labels = config.setdefault("Labels", {})
        labels[self.config.injected_label] = "true"
        labels[self.config.version_label] = "0.1.0"

        # 更新入口点
        config["Entrypoint"] = ["python3", self.config.wrapper_install_path]
        config["Cmd"] = []

        # 更新rootfs的diff_ids - 添加新layer的diff_id
        rootfs = self._image_config.setdefault("rootfs", {"type": "layers", "diff_ids": []})
        rootfs["diff_ids"].append(new_layer_diff_id)

        # 更新history
        history = self._image_config.setdefault("history", [])
        history.append({
            "created_by": "satcontainer inject - add checkpoint wrapper",
            "comment": "Added checkpoint wrapper for satellite container optimization",
            "empty_layer": False,
        })
