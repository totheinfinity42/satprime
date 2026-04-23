"""checkpoint_wrapper 单元测试"""

import ast
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from satcontainer.injector.wrapper.checkpoint_wrapper import CheckpointWrapper


class TestCheckpointWrapper(unittest.TestCase):
    """CheckpointWrapper 测试"""

    def setUp(self):
        """设置测试环境"""
        self.original_env = os.environ.copy()
        os.environ["ORIGINAL_ENTRYPOINT"] = '["python"]'
        os.environ["ORIGINAL_CMD"] = '["app.py"]'

    def tearDown(self):
        """恢复环境"""
        os.environ.clear()
        os.environ.update(self.original_env)

    def test_init_parses_env(self):
        """测试环境变量解析"""
        wrapper = CheckpointWrapper()
        self.assertEqual(wrapper.original_entrypoint, ["python"])
        self.assertEqual(wrapper.original_cmd, ["app.py"])
        self.assertFalse(wrapper.checkpoint_enabled)

    def test_checkpoint_enabled(self):
        """测试检查点模式启用"""
        os.environ["CHECKPOINT_ENABLED"] = "1"
        wrapper = CheckpointWrapper()
        self.assertTrue(wrapper.checkpoint_enabled)

    def test_find_python_script_simple(self):
        """测试简单的脚本查找"""
        wrapper = CheckpointWrapper()

        # python script.py
        result = wrapper.find_python_script(["python", "app.py"])
        self.assertEqual(result, "app.py")

        # python3 script.py
        result = wrapper.find_python_script(["python3", "main.py"])
        self.assertEqual(result, "main.py")

    def test_find_python_script_with_flags(self):
        """测试带参数的脚本查找"""
        wrapper = CheckpointWrapper()

        # python -u script.py
        result = wrapper.find_python_script(["python", "-u", "app.py"])
        self.assertEqual(result, "app.py")

        # python -B -O script.py
        result = wrapper.find_python_script(["python", "-B", "-O", "main.py"])
        self.assertEqual(result, "main.py")

    def test_find_python_script_module_mode(self):
        """测试模块模式返回None"""
        wrapper = CheckpointWrapper()

        # python -m module
        result = wrapper.find_python_script(["python", "-m", "pytest"])
        self.assertIsNone(result)

    def test_analyze_imports(self):
        """测试import分析"""
        wrapper = CheckpointWrapper()

        # 创建临时Python文件
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write("""
import torch
import numpy as np
from PIL import Image
from collections import defaultdict
import os.path
from torchvision.models import resnet50
""")
            temp_path = f.name

        try:
            modules = wrapper.analyze_imports(temp_path)
            self.assertIn("torch", modules)
            self.assertIn("numpy", modules)
            self.assertIn("PIL", modules)
            self.assertIn("collections", modules)
            self.assertIn("os", modules)
            self.assertIn("torchvision", modules)
        finally:
            os.unlink(temp_path)

    def test_analyze_imports_relative(self):
        """测试相对导入不会报错"""
        wrapper = CheckpointWrapper()

        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write("""
from . import utils
from ..common import helper
import torch
""")
            temp_path = f.name

        try:
            modules = wrapper.analyze_imports(temp_path)
            self.assertIn("torch", modules)
            # 相对导入不应该包含在结果中
        finally:
            os.unlink(temp_path)

    def test_analyze_imports_syntax_error(self):
        """测试语法错误处理"""
        wrapper = CheckpointWrapper()

        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write("def invalid syntax here")
            temp_path = f.name

        try:
            modules = wrapper.analyze_imports(temp_path)
            self.assertEqual(modules, [])  # 应该返回空列表
        finally:
            os.unlink(temp_path)


class TestImportAnalysis(unittest.TestCase):
    """Import分析边界情况测试"""

    def test_nested_imports(self):
        """测试嵌套模块导入"""
        wrapper = CheckpointWrapper()

        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write("""
import torch.nn.functional as F
from torchvision.transforms.v2 import Compose
""")
            temp_path = f.name

        try:
            modules = wrapper.analyze_imports(temp_path)
            # 应该只返回顶层模块
            self.assertIn("torch", modules)
            self.assertIn("torchvision", modules)
            self.assertNotIn("torch.nn", modules)
            self.assertNotIn("torch.nn.functional", modules)
        finally:
            os.unlink(temp_path)


if __name__ == "__main__":
    unittest.main()
