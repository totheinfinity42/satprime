"""镜像注入逻辑实现 - 支持 OCI 和 Docker 格式的 tar 文件"""

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
    """镜像注入器 - 将wrapper程序注入到OCI/Docker格式的容器镜像tar文件中"""

    def __init__(
        self,
        input_tar: str,
        dockerfile: Optional[str] = None,
        config: Optional[Config] = None,
    ):
        """
        Args:
            input_tar: 输入镜像tar文件路径
            dockerfile: 可选的Dockerfile路径（用于辅助分析入口点）
            config: 配置对象，默认使用全局配置
        """
        self.input_tar = Path(input_tar)
        self.dockerfile = Path(dockerfile) if dockerfile else None
        self.config = config or default_config

        if not self.input_tar.exists():
            raise FileNotFoundError("Input tar file not found: {}".format(self.input_tar))

        self._is_oci = False
        self._manifest = None
        self._oci_index = None
        self._oci_manifest = None
        self._image_config = None
        self._config_digest = None

    def _detect_format(self, tar):
        # type: (tarfile.TarFile) -> bool
        """检测是否为 OCI 格式"""
        try:
            tar.getmember("oci-layout")
            return True
        except KeyError:
            return False

    def _load_oci_index(self, tar):
        # type: (tarfile.TarFile) -> dict
        """加载 OCI index.json"""
        index_file = tar.extractfile("index.json")
        if index_file is None:
            raise ValueError("index.json not found in OCI tar")
        return json.load(index_file)

    def _load_oci_manifest(self, tar, digest):
        # type: (tarfile.TarFile, str) -> dict
        """加载 OCI manifest"""
        # digest 格式: sha256:xxx
        blob_path = "blobs/sha256/{}".format(digest.split(":")[1])
        manifest_file = tar.extractfile(blob_path)
        if manifest_file is None:
            raise ValueError("OCI manifest not found: {}".format(blob_path))
        return json.load(manifest_file)

    def _load_docker_manifest(self, tar):
        # type: (tarfile.TarFile) -> list
        """加载 Docker manifest.json"""
        manifest_file = tar.extractfile("manifest.json")
        if manifest_file is None:
            raise ValueError("manifest.json not found in tar")
        return json.load(manifest_file)

    def _load_image_config(self, tar, config_digest):
        # type: (tarfile.TarFile, str) -> dict
        """加载镜像配置文件"""
        # 处理不同格式的路径
        if config_digest.startswith("sha256:"):
            config_path = "blobs/sha256/{}".format(config_digest.split(":")[1])
        elif config_digest.startswith("blobs/"):
            config_path = config_digest
        else:
            config_path = config_digest

        config_file = tar.extractfile(config_path)
        if config_file is None:
            raise ValueError("Config file not found: {}".format(config_path))
        return json.load(config_file)

    def get_original_entrypoint(self):
        # type: () -> tuple
        """返回原始的 (ENTRYPOINT, CMD)"""
        with tarfile.open(self.input_tar, "r") as tar:
            self._is_oci = self._detect_format(tar)

            if self._is_oci:
                self._oci_index = self._load_oci_index(tar)
                manifest_desc = self._oci_index["manifests"][0]
                self._oci_manifest = self._load_oci_manifest(tar, manifest_desc["digest"])
                self._config_digest = self._oci_manifest["config"]["digest"]
            else:
                self._manifest = self._load_docker_manifest(tar)
                self._config_digest = self._manifest[0]["Config"]

            self._image_config = self._load_image_config(tar, self._config_digest)

        config = self._image_config.get("config", {})
        entrypoint = config.get("Entrypoint") or []
        cmd = config.get("Cmd") or []
        return entrypoint, cmd

    def is_injected(self):
        # type: () -> bool
        """检查镜像是否已经被注入"""
        with tarfile.open(self.input_tar, "r") as tar:
            self._is_oci = self._detect_format(tar)

            if self._is_oci:
                self._oci_index = self._load_oci_index(tar)
                manifest_desc = self._oci_index["manifests"][0]
                self._oci_manifest = self._load_oci_manifest(tar, manifest_desc["digest"])
                config_digest = self._oci_manifest["config"]["digest"]
            else:
                self._manifest = self._load_docker_manifest(tar)
                config_digest = self._manifest[0]["Config"]

            self._image_config = self._load_image_config(tar, config_digest)

        labels = self._image_config.get("config", {}).get("Labels") or {}
        return labels.get(self.config.injected_label) == "true"

    def _get_wrapper_content(self):
        # type: () -> bytes
        """获取wrapper程序内容"""
        wrapper_path = Path(__file__).parent / "wrapper" / "checkpoint_wrapper.py"
        return wrapper_path.read_bytes()

    def _parse_dockerfile_entrypoint(self):
        # type: () -> tuple
        """从Dockerfile解析ENTRYPOINT和CMD（作为备用）"""
        if self.dockerfile is None or not self.dockerfile.exists():
            return None, None

        entrypoint = None
        cmd = None

        content = self.dockerfile.read_text()
        for line in content.splitlines():
            line = line.strip()
            if line.upper().startswith("ENTRYPOINT"):
                entrypoint = self._parse_dockerfile_instruction(line, "ENTRYPOINT")
            elif line.upper().startswith("CMD"):
                cmd = self._parse_dockerfile_instruction(line, "CMD")

        return entrypoint, cmd

    def _parse_dockerfile_instruction(self, line, instruction):
        # type: (str, str) -> list
        """解析Dockerfile指令"""
        value = line[len(instruction):].strip()

        if value.startswith("["):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                pass

        return ["sh", "-c", value]

    def _create_layer_tar(self, files):
        # type: (dict) -> bytes
        """创建一个新的layer tar（未压缩）"""
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

    def _calculate_digest(self, content):
        # type: (bytes) -> str
        """计算sha256摘要"""
        return hashlib.sha256(content).hexdigest()

    def inject(
        self,
        output_tar,
        force=False,
        tag_suffix="-wrapped",
    ):
        # type: (str, bool, str) -> str
        """
        执行注入，返回输出tar文件路径

        Args:
            output_tar: 输出tar文件路径
            force: 是否强制覆盖已存在的文件
            tag_suffix: 镜像标签后缀（默认 "-wrapped"），设为 None 或空字符串保持原名

        Returns:
            输出tar文件路径
        """
        output_path = Path(output_tar)

        if output_path.exists() and not force:
            raise FileExistsError(
                "Output file '{}' already exists. Use --force to overwrite.".format(output_tar)
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)

            # 解压原始tar
            with tarfile.open(self.input_tar, "r") as tar:
                tar.extractall(tmpdir)
                self._is_oci = self._detect_format(tar)

                if self._is_oci:
                    self._oci_index = self._load_oci_index(tar)
                    manifest_desc = self._oci_index["manifests"][0]
                    self._oci_manifest = self._load_oci_manifest(tar, manifest_desc["digest"])
                    self._config_digest = self._oci_manifest["config"]["digest"]
                else:
                    self._manifest = self._load_docker_manifest(tar)
                    self._config_digest = self._manifest[0]["Config"]

                self._image_config = self._load_image_config(tar, self._config_digest)

            # 获取原始入口点
            config = self._image_config.get("config", {})
            original_entrypoint = config.get("Entrypoint") or []
            original_cmd = config.get("Cmd") or []

            if not original_entrypoint and not original_cmd:
                df_entrypoint, df_cmd = self._parse_dockerfile_entrypoint()
                if df_entrypoint:
                    original_entrypoint = df_entrypoint
                if df_cmd:
                    original_cmd = df_cmd

            entrypoint_json = json.dumps(original_entrypoint)
            cmd_json = json.dumps(original_cmd)

            # 创建新layer
            wrapper_content = self._get_wrapper_content()
            wrapper_path = self.config.wrapper_install_path.lstrip("/")

            new_layer_tar = self._create_layer_tar({
                wrapper_path: wrapper_content,
            })

            # 计算 diff_id（未压缩 tar 的 sha256）
            new_layer_diff_id = "sha256:{}".format(self._calculate_digest(new_layer_tar))

            # 压缩新layer
            new_layer_gz = gzip.compress(new_layer_tar)
            new_layer_digest = "sha256:{}".format(self._calculate_digest(new_layer_gz))

            # 写入新layer到 blobs 目录
            blobs_dir = tmpdir / "blobs" / "sha256"
            blobs_dir.mkdir(parents=True, exist_ok=True)
            new_layer_blob_path = blobs_dir / new_layer_digest.split(":")[1]
            new_layer_blob_path.write_bytes(new_layer_gz)

            # 更新镜像配置
            self._update_image_config(entrypoint_json, cmd_json, new_layer_diff_id)

            # 写入新配置到 blobs
            new_config_json = json.dumps(self._image_config).encode()
            new_config_digest = "sha256:{}".format(self._calculate_digest(new_config_json))
            new_config_blob_path = blobs_dir / new_config_digest.split(":")[1]
            new_config_blob_path.write_bytes(new_config_json)

            # 删除旧配置文件
            if self._config_digest.startswith("sha256:"):
                old_config_path = blobs_dir / self._config_digest.split(":")[1]
            elif self._config_digest.startswith("blobs/"):
                old_config_path = tmpdir / self._config_digest
            else:
                old_config_path = tmpdir / self._config_digest

            if old_config_path.exists():
                old_config_path.unlink()

            # 获取原始镜像名用于生成新标签
            original_image_name = self._get_original_image_name()
            new_image_name = self._generate_new_image_name(original_image_name, tag_suffix)

            if self._is_oci:
                self._update_oci_format(
                    tmpdir, blobs_dir, new_config_digest, new_layer_digest,
                    len(new_config_json), len(new_layer_gz), new_image_name
                )
            else:
                self._update_docker_format(
                    tmpdir, new_config_digest, new_layer_digest, new_image_name
                )

            # 打包新tar
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with tarfile.open(output_path, "w") as tar:
                for item in tmpdir.iterdir():
                    tar.add(item, arcname=item.name)

        return str(output_path)

    def _get_original_image_name(self):
        # type: () -> str
        """获取原始镜像名称"""
        if self._is_oci and self._oci_index:
            manifest_desc = self._oci_index["manifests"][0]
            annotations = manifest_desc.get("annotations", {})
            name = annotations.get("io.containerd.image.name", "")
            if name:
                # 去掉 docker.io/library/ 前缀
                if name.startswith("docker.io/library/"):
                    name = name[len("docker.io/library/"):]
                return name

        if self._manifest:
            tags = self._manifest[0].get("RepoTags", [])
            if tags:
                return tags[0]

        return "injected:latest"

    def _generate_new_image_name(self, original_name, tag_suffix):
        # type: (str, str) -> str
        """生成新镜像名称"""
        if not tag_suffix:
            return original_name

        if ":" in original_name:
            name, version = original_name.rsplit(":", 1)
            return "{}{}:{}".format(name, tag_suffix, version)
        else:
            return "{}{}".format(original_name, tag_suffix)

    def _update_oci_format(self, tmpdir, blobs_dir, new_config_digest, new_layer_digest,
                           config_size, layer_size, new_image_name):
        # type: (Path, Path, str, str, int, int, str) -> None
        """更新 OCI 格式的元数据"""
        # 更新 OCI manifest
        self._oci_manifest["config"]["digest"] = new_config_digest
        self._oci_manifest["config"]["size"] = config_size

        self._oci_manifest["layers"].append({
            "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
            "digest": new_layer_digest,
            "size": layer_size,
        })

        # 写入新 manifest 到 blobs
        new_manifest_json = json.dumps(self._oci_manifest).encode()
        new_manifest_digest = "sha256:{}".format(self._calculate_digest(new_manifest_json))
        new_manifest_blob_path = blobs_dir / new_manifest_digest.split(":")[1]
        new_manifest_blob_path.write_bytes(new_manifest_json)

        # 删除旧 manifest
        old_manifest_digest = self._oci_index["manifests"][0]["digest"]
        old_manifest_path = blobs_dir / old_manifest_digest.split(":")[1]
        if old_manifest_path.exists():
            old_manifest_path.unlink()

        # 更新 index.json
        self._oci_index["manifests"][0]["digest"] = new_manifest_digest
        self._oci_index["manifests"][0]["size"] = len(new_manifest_json)

        # 更新 annotations 中的镜像名
        annotations = self._oci_index["manifests"][0].setdefault("annotations", {})
        annotations["io.containerd.image.name"] = "docker.io/library/{}".format(new_image_name)
        if ":" in new_image_name:
            annotations["org.opencontainers.image.ref.name"] = new_image_name.split(":")[1]

        (tmpdir / "index.json").write_text(json.dumps(self._oci_index, indent=2))

        # 同时更新 Docker 格式的 manifest.json（保持兼容）
        if (tmpdir / "manifest.json").exists():
            docker_manifest = json.loads((tmpdir / "manifest.json").read_text())
            docker_manifest[0]["Config"] = "blobs/sha256/{}".format(new_config_digest.split(":")[1])
            docker_manifest[0]["Layers"].append("blobs/sha256/{}".format(new_layer_digest.split(":")[1]))
            docker_manifest[0]["RepoTags"] = [new_image_name]
            (tmpdir / "manifest.json").write_text(json.dumps(docker_manifest, indent=2))

    def _update_docker_format(self, tmpdir, new_config_digest, new_layer_digest, new_image_name):
        # type: (Path, str, str, str) -> None
        """更新 Docker 格式的 manifest.json"""
        self._manifest[0]["Config"] = new_config_digest
        self._manifest[0]["Layers"].append(new_layer_digest)
        self._manifest[0]["RepoTags"] = [new_image_name]

        (tmpdir / "manifest.json").write_text(json.dumps(self._manifest, indent=2))

    def _update_image_config(self, entrypoint_json, cmd_json, new_layer_diff_id):
        # type: (str, str, str) -> None
        """更新镜像配置"""
        config = self._image_config.setdefault("config", {})

        # 更新环境变量
        env = config.setdefault("Env", [])
        env = [e for e in env if not e.startswith("{}=".format(self.config.original_entrypoint_env))
               and not e.startswith("{}=".format(self.config.original_cmd_env))]
        env.append("{}={}".format(self.config.original_entrypoint_env, entrypoint_json))
        env.append("{}={}".format(self.config.original_cmd_env, cmd_json))
        config["Env"] = env

        # 更新Labels
        labels = config.setdefault("Labels", {})
        if labels is None:
            labels = {}
            config["Labels"] = labels
        labels[self.config.injected_label] = "true"
        labels[self.config.version_label] = "0.1.0"

        # 更新入口点
        config["Entrypoint"] = ["python3", self.config.wrapper_install_path]
        config["Cmd"] = []

        # 更新 rootfs diff_ids
        rootfs = self._image_config.setdefault("rootfs", {"type": "layers", "diff_ids": []})
        rootfs["diff_ids"].append(new_layer_diff_id)

        # 更新 history
        history = self._image_config.setdefault("history", [])
        history.append({
            "created_by": "satcontainer inject - add checkpoint wrapper",
            "comment": "Added checkpoint wrapper for satellite container optimization",
            "empty_layer": False,
        })
