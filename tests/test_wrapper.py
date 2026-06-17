"""Tests for the checkpoint wrapper."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from satecode.agent import CheckpointWrapper


class TestCheckpointWrapper(unittest.TestCase):

    def setUp(self):
        self.original_env = os.environ.copy()
        os.environ["ORIGINAL_ENTRYPOINT"] = '["python"]'
        os.environ["ORIGINAL_CMD"] = '["app.py"]'

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.original_env)

    def test_init_parses_env(self):
        wrapper = CheckpointWrapper()
        self.assertEqual(wrapper.original_entrypoint, ["python"])
        self.assertEqual(wrapper.original_cmd, ["app.py"])
        self.assertFalse(wrapper.checkpoint_enabled)

    def test_checkpoint_enabled(self):
        os.environ["CHECKPOINT_ENABLED"] = "1"
        wrapper = CheckpointWrapper()
        self.assertTrue(wrapper.checkpoint_enabled)

    def test_find_python_script_simple(self):
        wrapper = CheckpointWrapper()

        path, args, module = wrapper.find_python_script(["python", "app.py"])
        self.assertEqual(path, "app.py")
        self.assertEqual(args, [])
        self.assertIsNone(module)

        path, args, module = wrapper.find_python_script(["python3", "main.py"])
        self.assertEqual(path, "main.py")
        self.assertEqual(args, [])
        self.assertIsNone(module)

    def test_find_python_script_with_flags(self):
        wrapper = CheckpointWrapper()

        path, args, module = wrapper.find_python_script(["python", "-u", "app.py"])
        self.assertEqual(path, "app.py")
        self.assertEqual(args, [])
        self.assertIsNone(module)

        path, args, module = wrapper.find_python_script(["python", "-B", "-O", "main.py"])
        self.assertEqual(path, "main.py")
        self.assertEqual(args, [])
        self.assertIsNone(module)

    def test_find_python_script_module_mode(self):
        wrapper = CheckpointWrapper()
        path, args, module = wrapper.find_python_script(["python", "-m", "json.tool", "test.json"])
        self.assertIsNotNone(path)
        self.assertTrue(path.endswith(".py"))
        self.assertEqual(module, "json.tool")
        self.assertEqual(args, ["test.json"])

    def test_find_python_script_invalid_module(self):
        wrapper = CheckpointWrapper()
        path, args, module = wrapper.find_python_script(["python", "-m", "non_existent_module_xyz"])
        self.assertIsNone(path)
        self.assertIsNone(module)

    def test_analyze_imports(self):
        wrapper = CheckpointWrapper()

        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write(
                "import torch\n"
                "import numpy as np\n"
                "from PIL import Image\n"
                "from collections import defaultdict\n"
                "import os.path\n"
                "from torchvision.models import resnet50\n"
            )
            temp_path = f.name

        try:
            modules = wrapper.analyze_imports(temp_path)
            self.assertIn("torch", modules)
            self.assertIn("numpy", modules)
            self.assertIn("PIL", modules)
            self.assertIn("collections", modules)
            self.assertIn("os.path", modules)
            self.assertIn("torchvision.models", modules)
        finally:
            os.unlink(temp_path)

    def test_analyze_imports_relative(self):
        wrapper = CheckpointWrapper()

        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write("from . import utils\nfrom ..common import helper\nimport torch\n")
            temp_path = f.name

        try:
            modules = wrapper.analyze_imports(temp_path)
            self.assertIn("torch", modules)
        finally:
            os.unlink(temp_path)

    def test_analyze_imports_syntax_error(self):
        wrapper = CheckpointWrapper()

        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write("def invalid syntax here")
            temp_path = f.name

        try:
            modules = wrapper.analyze_imports(temp_path)
            self.assertEqual(modules, [])
        finally:
            os.unlink(temp_path)


class TestImportAnalysis(unittest.TestCase):

    def test_nested_imports(self):
        wrapper = CheckpointWrapper()

        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write(
                "import torch.nn.functional as F\n"
                "from torchvision.transforms.v2 import Compose\n"
            )
            temp_path = f.name

        try:
            modules = wrapper.analyze_imports(temp_path)
            self.assertIn("torch.nn.functional", modules)
            self.assertIn("torchvision.transforms.v2", modules)
        finally:
            os.unlink(temp_path)


if __name__ == "__main__":
    unittest.main()
