#!/usr/bin/env python3
"""
船只检测基准测试程序 - 故意设计的初始化版本
用于测试系统在加载大型遥感依赖和深度学习模型时的性能
"""

import sys
import os
import time
import signal
from datetime import datetime

# --- 第一阶段：库导入 (耗时起点) ---
print(f"[{datetime.now().strftime('%H:%M:%S')}] [INFO] 正在初始化依赖库...", flush=True)
_import_start = time.time()

import torch
import torchvision
import numpy as np
from PIL import Image, ImageDraw, ImageFont
import argparse
import rasterio
import geopandas as gpd
from scipy import ndimage

# --- 第二阶段：模型预加载 ---
# 弃用轻量级 YOLO，改用带 FPN 的 Faster R-CNN ResNet-101 (参数量巨大)
from torchvision.models.detection import FasterRCNN_ResNet50_FPN_V2_Weights
from torchvision.models.detection import fasterrcnn_resnet50_fpn_v2
from torchvision import transforms

from rasterio.features import rasterize
from shapely.geometry import box

# 强制 Torch 进行算子初始化
_dummy_tensor = torch.zeros(1).to(torch.device('cuda') if torch.cuda.is_available() else 'cpu')


print(f"[{datetime.now().strftime('%H:%M:%S')}] [INFO] 正在预加载检测模型权重...", flush=True)

# 模拟在导入阶段就占用显存/内存的行为（单例模式预热）
DEVICE = torch.device('cuda') if torch.cuda.is_available() else torch.device('cpu')
GLOBAL_MODEL = fasterrcnn_resnet50_fpn_v2(weights=FasterRCNN_ResNet50_FPN_V2_Weights.DEFAULT)
GLOBAL_MODEL.to(DEVICE)
GLOBAL_MODEL.eval()

_import_end = time.time()
print(f"[{datetime.now().strftime('%H:%M:%S')}] [SUCCESS] 核心环境加载完成，耗时: {_import_end - _import_start:.2f} 秒\n")


def checkpoint_barrier():
    # 只在你需要的时候启用，避免线上一直挂住
    barrier_value = os.environ.get("CHECKPOINT_BARRIER", "0")
    print(f"[DEBUG] CHECKPOINT_BARRIER = '{barrier_value}'", flush=True)
    
    if barrier_value != "1":
        print("[DEBUG] Checkpoint barrier disabled, continuing...", flush=True)
        return

    print("[BARRIER] ready for checkpoint, sending SIGSTOP to self", flush=True)
    os.kill(os.getpid(), signal.SIGSTOP)  # 进程会停住，直到外部 SIGCONT
    print("[BARRIER] resumed after SIGCONT", flush=True)


class ShipDetectionResult:
    """船只检测结果类"""
    def __init__(self, x, y, width, height, probability):
        self.x = x
        self.y = y
        self.width = width
        self.height = height
        self.probability = probability
    
    def __repr__(self):
        return (f"Ship {{ x: {int(self.x)}, y: {int(self.y)}, "
                f"width: {int(self.width)}, height: {int(self.height)}, "
                f"confidence: {self.probability:.4f} }}")


def pre_process_image(image_path, target_size=800):
    """预处理：支持大图切片逻辑和地理信息读取"""
    # 尝试使用 rasterio 读取地理信息（支持 GeoTIFF）
    geo_transform = None
    try:
        with rasterio.open(image_path) as src:
            geo_transform = src.transform
            # 读取波段数据并转换为 PIL Image
            if src.count >= 3:
                img_array = np.dstack([src.read(i+1) for i in range(min(3, src.count))])
                # 归一化到 0-255
                img_array = ((img_array - img_array.min()) / (img_array.max() - img_array.min()) * 255).astype(np.uint8)
                img = Image.fromarray(img_array, mode='RGB')
            else:
                # 如果波段不足，回退到 PIL
                img = Image.open(image_path).convert('RGB')
    except (rasterio.errors.RasterioIOError, AttributeError):
        # 如果不是地理图像，使用 PIL 打开
        img = Image.open(image_path).convert('RGB')
    
    original_size = img.size
    
    # 使用 scipy.ndimage 进行图像增强（边缘保持平滑）
    img_array = np.array(img)
    # 应用高斯滤波降噪
    img_array = ndimage.gaussian_filter(img_array, sigma=0.5)
    img = Image.fromarray(img_array.astype(np.uint8))
    
    # 转换为适合 Faster R-CNN 的 Tensor 格式
    transform = transforms.Compose([
        transforms.Resize((target_size, target_size)),
        transforms.ToTensor(),
    ])
    
    img_tensor = transform(img)
    scale_x = original_size[0] / target_size
    scale_y = original_size[1] / target_size
    
    return img_tensor, (scale_x, scale_y), original_size, geo_transform


def post_process_results(predictions, conf_threshold, scales):
    """
    针对 Faster R-CNN 输出的后处理，使用 torchvision.ops.nms 进行非极大值抑制
    """
    # Faster R-CNN 输出格式为 [{'boxes': tensor, 'labels': tensor, 'scores': tensor}]
    pred = predictions[0]
    boxes = pred['boxes']
    scores = pred['scores']
    labels = pred['labels']
    
    # 使用 torchvision.ops.nms 进行更精确的非极大值抑制
    keep_indices = torchvision.ops.nms(boxes, scores, iou_threshold=0.5)
    
    # 过滤结果
    boxes = boxes[keep_indices].cpu().numpy()
    scores = scores[keep_indices].cpu().numpy()
    labels = labels[keep_indices].cpu().numpy()
    
    sx, sy = scales
    results = []
    
    for i in range(len(scores)):
        # COCO 类别中，船 (boat) 通常是 9
        if scores[i] >= conf_threshold:
            x1, y1, x2, y2 = boxes[i]
            results.append(ShipDetectionResult(
                x1 * sx, y1 * sy, (x2 - x1) * sx, (y2 - y1) * sy, scores[i]
            ))
            
    return results


def draw_detections(image_path, results, output_path):
    img = Image.open(image_path).convert('RGB')
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 25)
    except:
        font = ImageFont.load_default()
    
    for result in results:
        shape = [result.x, result.y, result.x + result.width, result.y + result.height]
        draw.rectangle(shape, outline='red', width=4)
        draw.text((result.x, result.y - 30), f"SHIP {result.probability:.2f}", fill='red', font=font)
    
    img.save(output_path)
    print(f"标注图像已存至: {output_path}")


def save_detections_to_geojson(results, image_path, output_path, geo_transform=None):
    """使用 geopandas 将检测结果保存为 GeoJSON 格式"""
    if not results:
        return
    
    # 创建几何对象列表
    geometries = []
    properties_list = []
    
    for i, result in enumerate(results):
        # 创建边界框几何
        minx = result.x
        miny = result.y
        maxx = result.x + result.width
        maxy = result.y + result.height
        
        geom = box(minx, miny, maxx, maxy)
        geometries.append(geom)
        
        # 添加属性
        properties_list.append({
            'id': i + 1,
            'confidence': float(result.probability),
            'class': 'ship',
            'image': os.path.basename(image_path)
        })
    
    # 创建 GeoDataFrame
    gdf = gpd.GeoDataFrame(properties_list, geometry=geometries)
    
    # 保存为 GeoJSON
    gdf.to_file(output_path, driver='GeoJSON')
    print(f"GeoJSON 已保存至: {output_path}")


def create_detection_mask(results, image_size, output_path):
    """使用 rasterio.features 将检测结果栅格化为掩码图像"""
    if not results:
        return
    
    width, height = image_size
    
    # 创建几何对象和对应的值（使用置信度作为像素值）
    shapes = []
    for result in results:
        minx = int(result.x)
        miny = int(result.y)
        maxx = int(result.x + result.width)
        maxy = int(result.y + result.height)
        
        geom = box(minx, miny, maxx, maxy)
        # 将置信度映射到 1-255 范围
        value = int(result.probability * 255)
        shapes.append((geom, value))
    
    # 栅格化：将矢量几何转换为栅格掩码
    mask = rasterize(
        shapes,
        out_shape=(height, width),
        fill=0,
        dtype=np.uint8
    )
    
    # 保存为 GeoTIFF
    with rasterio.open(
        output_path,
        'w',
        driver='GTiff',
        height=height,
        width=width,
        count=1,
        dtype=np.uint8,
        compress='lzw'
    ) as dst:
        dst.write(mask, 1)
    
    print(f"检测掩码已保存至: {output_path}")


def get_image_files(directory):
    """获取目录下所有图片文件"""
    image_extensions = {'.jpg', '.jpeg', '.png', '.bmp', '.tif', '.tiff'}
    image_files = []
    
    if os.path.isfile(directory):
        # 如果是单个文件，直接返回
        return [directory]
    
    for root, dirs, files in os.walk(directory):
        for file in files:
            if os.path.splitext(file)[1].lower() in image_extensions:
                image_files.append(os.path.join(root, file))
    
    return sorted(image_files)


# 全局标志用于优雅退出
should_exit = False

def signal_handler(sig, frame):
    """处理 Ctrl+C 信号"""
    global should_exit
    print("\n\n[收到退出信号] 正在停止循环...")
    should_exit = True


def main():
    global should_exit
    
    # 注册信号处理器
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    checkpoint_barrier()
    
    start_timestamp_ms = int(time.time() * 1000)
    
    parser = argparse.ArgumentParser(description='卫星图像船只检测基准程序')
    parser.add_argument('model_path', type=str, nargs='?', default="builtin", help='模型路径 (本程序默认使用内置 ResNet101)')
    parser.add_argument('input_path', type=str, help='输入图像路径或目录')
    parser.add_argument('--output-dir', type=str, default='output', help='输出目录 (默认: output)')
    parser.add_argument('--confidence', type=float, default=0.6, help='置信度阈值')
    parser.add_argument('--iterations', type=int, default=None, help='推理迭代次数（图片遍历完后循环）')
    parser.add_argument('--continuous', action='store_true', help='持续循环模式，按 Ctrl+C 退出')
    parser.add_argument('--save-images', action='store_true', help='是否保存标注图像（默认不保存以提升性能）')
    parser.add_argument('--save-geojson', action='store_true', help='保存检测结果为 GeoJSON 格式')
    parser.add_argument('--save-mask', action='store_true', help='保存检测掩码为 GeoTIFF 格式')
    
    args = parser.parse_args()
    
    # 如果既没有指定 iterations 也没有指定 continuous，默认为 1 次
    if args.iterations is None and not args.continuous:
        args.iterations = 1
    
    if not os.path.exists(args.input_path):
        print(f"错误: 找不到路径 {args.input_path}")
        sys.exit(1)

    # 获取所有图片文件
    image_files = get_image_files(args.input_path)
    if not image_files:
        print(f"错误: 在 {args.input_path} 中未找到图片文件")
        sys.exit(1)
    
    # 创建输出目录
    if args.save_images or args.save_geojson or args.save_mask:
        os.makedirs(args.output_dir, exist_ok=True)

    print(f"测试任务开始...")
    print(f"输入路径: {args.input_path}")
    print(f"发现图片: {len(image_files)} 张")
    print(f"输出目录: {args.output_dir}")
    print(f"计算设备: {DEVICE}")
    if args.continuous:
        print(f"模式: 持续循环 (按 Ctrl+C 退出)")
    else:
        print(f"模式: 固定迭代 ({args.iterations} 次)")
    print(f"保存图像: {'是' if args.save_images else '否'}")
    print(f"保存GeoJSON: {'是' if args.save_geojson else '否'}")
    print(f"保存掩码: {'是' if args.save_mask else '否'}")

    total_inference_time = 0
    processed_count = 0
    iteration = 0
    
    print(f"\n执行推理循环...")
    print("=" * 80)

    # 循环推理：根据模式选择固定次数或无限循环
    try:
        while not should_exit:
            # 计算当前应该处理哪张图片（循环遍历）
            img_idx = iteration % len(image_files)
            image_path = image_files[img_idx]
            
            # 准备输出路径
            base_name = os.path.basename(image_path)
            name_without_ext = os.path.splitext(base_name)[0]
            ext = os.path.splitext(base_name)[1]
            output_path = os.path.join(args.output_dir, f"{name_without_ext}_detected{ext}")
            
            print(f"\n[迭代 {iteration + 1}" + (f"/{args.iterations}" if args.iterations else "") + f"] 处理图片: {base_name} (第 {img_idx + 1}/{len(image_files)} 张)")
            
            # 预处理
            try:
                img_tensor, scales, original_size, geo_transform = pre_process_image(image_path)
                input_batch = [img_tensor.to(DEVICE)]
            except Exception as e:
                print(f"  ✗ 预处理失败: {e}")
                iteration += 1
                # 固定迭代模式下检查是否已完成
                if not args.continuous and args.iterations and iteration >= args.iterations:
                    break
                continue
            
            # 推理
            t0 = time.time()
            with torch.no_grad():
                output = GLOBAL_MODEL(input_batch)
            t1 = time.time()
            
            iter_time = t1 - t0
            total_inference_time += iter_time
            
            # 后处理
            results = post_process_results(output, args.confidence, scales)
            
            print(f"  推理耗时: {iter_time:.4f}s")
            print(f"  检测到船只: {len(results)} 艘")
            
            # 显示前3个结果
            for j, res in enumerate(results[:3]):
                print(f"    {j+1}. {res}")
            if len(results) > 3:
                print(f"    ... 还有 {len(results) - 3} 个结果")
            
            # 保存结果（可选）
            if args.save_images and len(results) > 0:
                try:
                    draw_detections(image_path, results, output_path)
                    print(f"  ✓ 结果已保存: {output_path}")
                except Exception as e:
                    print(f"  ✗ 保存失败: {e}")
            
            # 保存 GeoJSON（可选）
            if args.save_geojson and len(results) > 0:
                try:
                    geojson_path = os.path.join(args.output_dir, f"{name_without_ext}_detections.geojson")
                    save_detections_to_geojson(results, image_path, geojson_path, geo_transform)
                except Exception as e:
                    print(f"  ✗ GeoJSON 保存失败: {e}")
            
            # 保存检测掩码（可选）
            if args.save_mask and len(results) > 0:
                try:
                    mask_path = os.path.join(args.output_dir, f"{name_without_ext}_mask.tif")
                    create_detection_mask(results, original_size, mask_path)
                except Exception as e:
                    print(f"  ✗ 掩码保存失败: {e}")
            
            processed_count += 1
            iteration += 1
            
            # 显示统计信息（每10次）
            if iteration % 10 == 0:
                avg_time = total_inference_time / processed_count if processed_count > 0 else 0
                throughput = processed_count / total_inference_time if total_inference_time > 0 else 0
                print(f"\n  [统计] 已处理 {processed_count} 次 | 平均耗时: {avg_time:.4f}s | 吞吐量: {throughput:.2f} 次/秒")
            
            # 固定迭代模式下检查是否已完成
            if not args.continuous and args.iterations and iteration >= args.iterations:
                break
    
    except KeyboardInterrupt:
        print("\n\n[键盘中断] 正在停止...")
    
    # 性能汇总
    print("\n" + "=" * 80)
    print(f"处理完成统计:")
    print(f"  总迭代次数: {iteration}")
    print(f"  成功处理: {processed_count} 次")
    print(f"  不同图片数: {len(image_files)} 张")
    print(f"  完整循环: {iteration // len(image_files)} 轮")
    print(f"  平均推理耗时: {total_inference_time / processed_count:.4f}s" if processed_count > 0 else "  无有效推理")
    print(f"  总推理时间: {total_inference_time:.2f}s")
    print(f"  系统总运行时: {(time.time() * 1000 - start_timestamp_ms)/1000:.2f}s (含冷启动导入)")
    print(f"  吞吐量: {processed_count / total_inference_time:.2f} 次/秒" if total_inference_time > 0 else "  N/A")

if __name__ == "__main__":
    main()