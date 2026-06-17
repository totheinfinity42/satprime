"""Tests for ImageInjector."""

import gzip
import hashlib
import io
import json
import os
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from satecode.runtime import ImagePatcher as ImageInjector


def create_test_image_tar(tmpdir: Path, entrypoint: list = None, cmd: list = None) -> Path:
    """Build a minimal Docker-format image tar for testing."""
    image_config = {
        "config": {
            "Entrypoint": entrypoint,
            "Cmd": cmd,
            "Env": ["PATH=/usr/local/bin:/usr/bin:/bin"],
            "Labels": {},
        },
        "rootfs": {"type": "layers", "diff_ids": []},
        "history": [],
    }

    config_json = json.dumps(image_config).encode()
    config_digest = hashlib.sha256(config_json).hexdigest()
    config_filename = f"{config_digest}.json"

    layer_buffer = io.BytesIO()
    with tarfile.open(fileobj=layer_buffer, mode="w") as layer_tar:
        info = tarfile.TarInfo(name="app/dummy.txt")
        content = b"test content"
        info.size = len(content)
        layer_tar.addfile(info, io.BytesIO(content))

    layer_data = layer_buffer.getvalue()
    layer_gz = gzip.compress(layer_data)
    layer_digest = hashlib.sha256(layer_gz).hexdigest()
    layer_filename = f"{layer_digest}.tar.gz"

    manifest = [{
        "Config": config_filename,
        "RepoTags": ["test:latest"],
        "Layers": [layer_filename],
    }]

    tar_path = tmpdir / "test_image.tar"
    with tarfile.open(tar_path, "w") as tar:
        config_info = tarfile.TarInfo(name=config_filename)
        config_info.size = len(config_json)
        tar.addfile(config_info, io.BytesIO(config_json))

        layer_info = tarfile.TarInfo(name=layer_filename)
        layer_info.size = len(layer_gz)
        tar.addfile(layer_info, io.BytesIO(layer_gz))

        manifest_json = json.dumps(manifest).encode()
        manifest_info = tarfile.TarInfo(name="manifest.json")
        manifest_info.size = len(manifest_json)
        tar.addfile(manifest_info, io.BytesIO(manifest_json))

    return tar_path


class TestImageInjector(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.tmpdir_path = Path(self.tmpdir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir)

    def test_get_original_entrypoint(self):
        tar_path = create_test_image_tar(
            self.tmpdir_path, entrypoint=["python"], cmd=["app.py", "--flag"]
        )
        injector = ImageInjector(str(tar_path))
        entrypoint, cmd = injector.get_original_entrypoint()
        self.assertEqual(entrypoint, ["python"])
        self.assertEqual(cmd, ["app.py", "--flag"])

    def test_get_original_entrypoint_none(self):
        tar_path = create_test_image_tar(self.tmpdir_path, entrypoint=None, cmd=None)
        injector = ImageInjector(str(tar_path))
        entrypoint, cmd = injector.get_original_entrypoint()
        self.assertEqual(entrypoint, [])
        self.assertEqual(cmd, [])

    def test_is_injected_false(self):
        tar_path = create_test_image_tar(self.tmpdir_path)
        self.assertFalse(ImageInjector(str(tar_path)).is_injected())

    def test_inject_creates_new_tar(self):
        tar_path = create_test_image_tar(
            self.tmpdir_path, entrypoint=["python"], cmd=["app.py"]
        )
        output_path = self.tmpdir_path / "output.tar"
        result = ImageInjector(str(tar_path)).inject(str(output_path))
        self.assertEqual(result, str(output_path))
        self.assertTrue(output_path.exists())

    def test_inject_modifies_entrypoint(self):
        tar_path = create_test_image_tar(
            self.tmpdir_path, entrypoint=["python"], cmd=["app.py"]
        )
        output_path = self.tmpdir_path / "output.tar"
        ImageInjector(str(tar_path)).inject(str(output_path))

        entrypoint, cmd = ImageInjector(str(output_path)).get_original_entrypoint()
        self.assertEqual(entrypoint, ["python3", "/opt/satcontainer/checkpoint_wrapper.py"])
        self.assertEqual(cmd, [])

    def test_inject_sets_env_vars(self):
        tar_path = create_test_image_tar(
            self.tmpdir_path, entrypoint=["python"], cmd=["app.py"]
        )
        output_path = self.tmpdir_path / "output.tar"
        ImageInjector(str(tar_path)).inject(str(output_path))

        with tarfile.open(output_path, "r") as tar:
            manifest = json.load(tar.extractfile("manifest.json"))
            config = json.load(tar.extractfile(manifest[0]["Config"]))

        env_dict = dict(e.split("=", 1) for e in config.get("config", {}).get("Env", []))
        self.assertIn("ORIGINAL_ENTRYPOINT", env_dict)
        self.assertIn("ORIGINAL_CMD", env_dict)
        self.assertEqual(json.loads(env_dict["ORIGINAL_ENTRYPOINT"]), ["python"])
        self.assertEqual(json.loads(env_dict["ORIGINAL_CMD"]), ["app.py"])

    def test_inject_marks_as_injected(self):
        tar_path = create_test_image_tar(self.tmpdir_path)
        output_path = self.tmpdir_path / "output.tar"
        ImageInjector(str(tar_path)).inject(str(output_path))
        self.assertTrue(ImageInjector(str(output_path)).is_injected())

    def test_inject_adds_wrapper_layer(self):
        tar_path = create_test_image_tar(self.tmpdir_path)
        output_path = self.tmpdir_path / "output.tar"
        ImageInjector(str(tar_path)).inject(str(output_path))

        with tarfile.open(output_path, "r") as tar:
            manifest = json.load(tar.extractfile("manifest.json"))
            self.assertEqual(len(manifest[0]["Layers"]), 2)

    def test_inject_refuses_overwrite(self):
        tar_path = create_test_image_tar(self.tmpdir_path)
        output_path = self.tmpdir_path / "output.tar"
        output_path.write_bytes(b"existing")
        with self.assertRaises(FileExistsError):
            ImageInjector(str(tar_path)).inject(str(output_path))

    def test_inject_force_overwrite(self):
        tar_path = create_test_image_tar(self.tmpdir_path)
        output_path = self.tmpdir_path / "output.tar"
        output_path.write_bytes(b"existing")
        result = ImageInjector(str(tar_path)).inject(str(output_path), force=True)
        self.assertEqual(result, str(output_path))

    def test_file_not_found(self):
        with self.assertRaises(FileNotFoundError):
            ImageInjector("/nonexistent/path.tar")


class TestDockerfileParsing(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.tmpdir_path = Path(self.tmpdir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir)

    def test_parse_dockerfile_json_format(self):
        tar_path = create_test_image_tar(self.tmpdir_path)
        dockerfile = self.tmpdir_path / "Dockerfile"
        dockerfile.write_text('ENTRYPOINT ["python", "app.py"]\nCMD ["--help"]')

        injector = ImageInjector(str(tar_path), dockerfile=str(dockerfile))
        entrypoint, cmd = injector._parse_dockerfile_entrypoint()
        self.assertEqual(entrypoint, ["python", "app.py"])
        self.assertEqual(cmd, ["--help"])

    def test_parse_dockerfile_shell_format(self):
        tar_path = create_test_image_tar(self.tmpdir_path)
        dockerfile = self.tmpdir_path / "Dockerfile"
        dockerfile.write_text("ENTRYPOINT python app.py")

        injector = ImageInjector(str(tar_path), dockerfile=str(dockerfile))
        entrypoint, cmd = injector._parse_dockerfile_entrypoint()
        self.assertEqual(entrypoint, ["sh", "-c", "python app.py"])


if __name__ == "__main__":
    unittest.main()
