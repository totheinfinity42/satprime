# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This project implements container startup optimization for satellite computing applications. It addresses the challenge of frequent cold starts in satellite computational payloads (Raspberry Pi, Jetson Nano) that operate under windowed power supply constraints.

The framework optimizes two phases:
- **Build phase**: Generate optimized container images with automatic checkpoint discovery and EROFS-based layout optimization
- **Execution phase**: Minimize power-on to application-ready time via fast cache warming and checkpoint restore

## Architecture

### Core Concepts

1. **Checkpoint Auto-Discovery**: Wrapper program injection replaces container entrypoint, handles dependency imports, detects checkpoint-ready state, and passes arguments to the actual entry program

2. **Image Layout Optimization**:
   - Analyze checkpoint restore process to build hot file list
   - Package rootfs + checkpoint artifacts as read-only EROFS layer
   - Reorder files to place hot files at the beginning for sequential prefetch

3. **Fast Cache Warm**: Preheater runs early during boot, sequentially reads hot files to populate page cache in parallel with system/runtime initialization

4. **Checkpoint Barrier**: Applications can use `CHECKPOINT_ENABLED=1` environment variable to pause via SIGSTOP when ready for checkpointing

---

## SatContainer 工具使用文档

### 1. 镜像注入 (Injector)

将 checkpoint wrapper 注入到 OCI 格式的容器镜像 tar 文件中。

#### 1.1 准备工作

首先需要将 Docker 镜像导出为 tar 文件：

```bash
# 从已有镜像导出
docker save myimage:latest -o myimage.tar

# 或者从 Dockerfile 构建后导出
docker build -t myimage:latest .
docker save myimage:latest -o myimage.tar
```

#### 1.2 执行注入

**CLI 方式：**

```bash
# 基本用法（默认添加 -wrapped 后缀）
python3 -m satcontainer inject \
    -i myimage.tar \
    -o myimage_injected.tar
# 加载后镜像名: myimage-wrapped:latest

# 自定义后缀
python3 -m satcontainer inject \
    -i myimage.tar \
    -o myimage_injected.tar \
    -s "-checkpoint"
# 加载后镜像名: myimage-checkpoint:latest

# 保持原名（不推荐，会覆盖原镜像）
python3 -m satcontainer inject \
    -i myimage.tar \
    -o myimage_injected.tar \
    -s ""

# 带 Dockerfile（用于辅助解析入口点）
python3 -m satcontainer inject \
    -i myimage.tar \
    -o myimage_injected.tar \
    -d Dockerfile

# 强制覆盖已存在的输出文件
python3 -m satcontainer inject \
    -i myimage.tar \
    -o myimage_injected.tar \
    -f
```

**Python API 方式：**

```python
from satcontainer.injector import ImageInjector

# 创建注入器
injector = ImageInjector(
    input_tar="myimage.tar",
    dockerfile="Dockerfile",  # 可选
)

# 执行注入（默认添加 -wrapped 后缀）
output_path = injector.inject(
    output_tar="myimage_injected.tar",
    force=True,           # 可选，强制覆盖
    tag_suffix="-wrapped", # 可选，镜像标签后缀
)

# 查看原始入口点
entrypoint, cmd = injector.get_original_entrypoint()
print(f"ENTRYPOINT: {entrypoint}")
print(f"CMD: {cmd}")

# 检查是否已注入
if injector.is_injected():
    print("Image already injected")
```

#### 1.3 查看镜像信息

```bash
python3 -m satcontainer inspect -i myimage.tar
```

输出示例：
```
Image: myimage.tar
ENTRYPOINT: ['python', 'demo/image_demo.py']
CMD: ['--device', 'cpu']
Status: Not injected
```

#### 1.4 加载注入后的镜像

```bash
docker load -i myimage_injected.tar
```

---

### 2. 使用注入后的镜像

#### 2.1 普通运行（不阻塞）

注入后的镜像可以像原始镜像一样运行，wrapper 会：
1. 自动分析 Python 脚本的 import 语句
2. 预加载所有依赖模块
3. 在同一进程内执行原始脚本（保留预加载的模块）

```bash
# 使用默认 CMD
docker run --rm myimage:latest

# 传入自定义参数（会替换默认 CMD）
docker run --rm myimage:latest arg1 arg2 --flag value
```

**示例（mmrotate）：**

```bash
docker run --rm -v $(pwd)/output:/mmrotate/output mmrotate:latest \
    demo/demo.jpg \
    oriented_rcnn_r50_fpn_1x_dota_le90.py \
    oriented_rcnn_r50_fpn_1x_dota_le90-6d2b2ce0.pth \
    --iterations 10 \
    --device cpu
```

#### 2.2 检查点模式（阻塞等待）

添加 `CHECKPOINT_ENABLED=1` 环境变量，wrapper 会在预加载完成后阻塞等待 `SIGUSR1` 信号。

```bash
# 启动容器（会阻塞等待 SIGUSR1）
docker run -d -e CHECKPOINT_ENABLED=1 --name mycontainer myimage:latest
```

**完整流程：**

```bash
# 1. 启动容器（会阻塞等待 SIGUSR1 信号）
docker run -d --name mycontainer -e CHECKPOINT_ENABLED=1 myimage:latest

# 2. 查看日志确认已阻塞
docker logs mycontainer
# 应该显示：[SatContainer] Waiting for SIGUSR1 signal to continue...

# 3. 执行检查点操作（使用 CRIU 或其他工具）
# ... 你的检查点操作 ...

# 4. 发送 SIGUSR1 恢复执行
docker kill -s SIGUSR1 mycontainer

# 5. 查看后续日志
docker logs -f mycontainer
# [SatContainer] Received SIGUSR1, continuing to original program...
```

#### 2.3 配置文件（用于 restore 时修改参数）

wrapper 支持从配置文件读取启动参数，用于 `ctr restore` 时修改参数（因为 restore 不支持修改命令行参数和环境变量）。

**配置文件路径：** `/etc/satcontainer/run.json`

**配置文件格式：**

```json
{
    "args": ["demo/demo.jpg", "config.py", "checkpoint.pth", "--iterations", "5", "--device", "cpu"],
    "env": {
        "CUDA_VISIBLE_DEVICES": "0",
        "DEBUG": "1"
    },
    "workdir": "/app"
}
```

**字段说明：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `args` | `string[]` | 脚本参数（替换命令行传入的参数） |
| `env` | `object` | 额外环境变量 |
| `workdir` | `string` | 工作目录 |

**参数优先级：**

1. 配置文件中的 `args`（最高优先级）
2. wrapper 的命令行参数
3. 原始镜像的 CMD

**使用流程（ctr checkpoint/restore）：**

```bash
# 1. 启动容器并创建检查点
sudo ctr run -d \
    --env CHECKPOINT_ENABLED=1 \
    docker.io/library/mmrotate-rpi-wrapped:latest \
    my-task \
    python3 /opt/satcontainer/checkpoint_wrapper.py \
    demo/demo.jpg config.py checkpoint.pth --iterations 10

# 2. 等待容器阻塞后，创建检查点
sudo ctr t checkpoint my-task checkpoint-v1

# 3. 删除原容器
sudo ctr t kill my-task
sudo ctr c rm my-task

# 4. 在 restore 之前，修改配置文件来改变启动参数
# 创建配置目录（需要挂载到容器内）
mkdir -p /tmp/satcontainer-config
cat > /tmp/satcontainer-config/run.json << 'EOF'
{
    "args": ["demo/demo2.jpg", "config.py", "checkpoint.pth", "--iterations", "5"],
    "env": {
        "DEBUG": "1"
    }
}
EOF

# 5. restore 时挂载配置目录
sudo ctr run --rm \
    --mount type=bind,src=/tmp/satcontainer-config,dst=/etc/satcontainer,options=rbind:ro \
    --checkpoint checkpoint-v1 \
    docker.io/library/mmrotate-rpi-wrapped:latest \
    my-task-restored

# 容器恢复后会使用配置文件中的新参数
```

**环境变量：**

| 环境变量 | 默认值 | 说明 |
|---------|--------|------|
| `SATCONTAINER_CONFIG_DIR` | `/etc/satcontainer` | 配置文件目录 |

#### 2.4 环境变量说明

| 环境变量 | 默认值 | 说明 |
|---------|--------|------|
| `CHECKPOINT_ENABLED` | (空) | 设为 `1` 启用检查点阻塞模式 |
| `CHECKPOINT_READY_FILE` | `/tmp/checkpoint_ready` | ready 标记文件路径 |
| `ORIGINAL_ENTRYPOINT` | (自动设置) | 原始 ENTRYPOINT（JSON 格式） |
| `ORIGINAL_CMD` | (自动设置) | 原始 CMD（JSON 格式） |

---

### 3. Wrapper 工作原理

```
容器启动 (wrapper 作为入口点)
  │
  ▼
解析环境变量，获取原始 ENTRYPOINT/CMD
  │
  ▼
构建完整命令（用户参数会替换 CMD）
  │
  ▼
找到 Python 脚本路径
  │
  ▼
AST 分析脚本，提取所有 import 语句
  │  - import torch
  │  - from PIL import Image
  │  - import numpy as np
  │
  ▼
动态导入所有分析到的模块（预加载）
  │
  ▼
检查 CHECKPOINT_ENABLED == "1"?
  │
  ├─ Yes ──▶ 创建 ready 标记文件
  │          │
  │          ▼
  │          os.kill(self, SIGSTOP) ──阻塞──
  │          │
  │          ▼ (SIGCONT 后继续)
  │          删除标记文件
  │
  ▼
使用 runpy.run_path() 在同一进程内执行脚本
（预加载的模块保留在 sys.modules 中）
```

---

## Demo Applications

### Ship Detection (`demo_apps/ship_detect/`)

Computer vision application for detecting ships in satellite imagery using Faster R-CNN ResNet50 FPN V2.

**Build the container:**
```bash
docker build -f demo_apps/ship_detect/Dockerfile.ship_detect -t ship:latest demo_apps/ship_detect/
```

**Run detection:**
```bash
docker run -v /path/to/images:/app/ship_image ship:latest python ship_detect_v2.py ship_image/ --confidence 0.3
```

**Key flags:**
- `--confidence`: Detection threshold (default: 0.6)
- `--iterations`: Number of inference iterations
- `--continuous`: Continuous loop mode (Ctrl+C to exit)
- `--save-images`: Save annotated output images
- `--save-geojson`: Export detections as GeoJSON
- `--save-mask`: Export detection mask as GeoTIFF

---

## Technology Stack

- **Language**: Python 3.7+ (wrapper 兼容旧版本)
- **ML Framework**: PyTorch, TorchVision
- **Geospatial**: rasterio, geopandas, shapely, fiona, pyproj
- **File System**: EROFS (Enhanced Read-Only File System)
- **Target Hardware**: ARM64 (Raspberry Pi, Jetson Nano), CPU inference

---

## 项目结构

```
sat-container/
├── satcontainer/                    # 主包
│   ├── __init__.py
│   ├── __main__.py                  # python -m satcontainer 入口
│   ├── cli.py                       # 统一 CLI 入口
│   ├── config.py                    # 全局配置管理
│   │
│   ├── injector/                    # 镜像注入模块
│   │   ├── __init__.py
│   │   ├── inject.py                # 注入逻辑（操作 OCI tar）
│   │   └── wrapper/
│   │       └── checkpoint_wrapper.py  # 容器内运行的 wrapper
│   │
│   ├── checkpoint/                  # 检查点制作（预留）
│   ├── analyzer/                    # 热点文件分析（预留）
│   ├── builder/                     # EROFS 镜像构建（预留）
│   └── preheater/                   # 预热程序（预留）
│
├── demo_apps/                       # 示例应用
├── tests/                           # 测试
└── pyproject.toml                   # 项目配置
```
