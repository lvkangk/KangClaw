"""图像处理工具：滤镜、水印、格式转换。"""

from __future__ import annotations

from pathlib import Path

from langchain_core.tools import tool
from PIL import Image, ImageDraw, ImageFilter, ImageFont


def _output_path(file_path: str, suffix: str, fmt: str = "") -> str:
    """生成输出文件路径：原路径_suffix.ext"""
    p = Path(file_path)
    ext = f".{fmt.lstrip('.')}" if fmt else p.suffix
    return str(p.with_stem(f"{p.stem}_{suffix}").with_suffix(ext))


def _done(out_path: str) -> str:
    """工具返回统一格式，提示 agent 用 send_image 发送。"""
    return f"处理完成，文件已保存到: {out_path}\n请调用 send_image 工具将图片发送给用户。"


_FILTERS = {
    "blur": lambda img: img.filter(ImageFilter.GaussianBlur(radius=3)),
    "sharpen": lambda img: img.filter(ImageFilter.SHARPEN),
    "contour": lambda img: img.filter(ImageFilter.CONTOUR),
    "emboss": lambda img: img.filter(ImageFilter.EMBOSS),
    "edge_enhance": lambda img: img.filter(ImageFilter.EDGE_ENHANCE),
    "grayscale": lambda img: img.convert("L"),
}


@tool
def image_filter(file_path: str, filter_name: str) -> str:
    """应用滤镜效果。

    Args:
        file_path: 图片文件路径
        filter_name: 滤镜名称，支持: blur, sharpen, grayscale, contour, emboss, edge_enhance
    """
    p = Path(file_path)
    if not p.exists():
        return f"错误：文件不存在 {file_path}"
    name = filter_name.lower().strip()
    if name not in _FILTERS:
        return f"不支持的滤镜: {name}。支持: {', '.join(_FILTERS.keys())}"
    try:
        with Image.open(p) as img:
            out = _FILTERS[name](img)
            out_path = _output_path(file_path, name)
            out.save(out_path)
            return _done(out_path)
    except Exception as e:
        return f"错误：{e}"


@tool
def image_watermark(file_path: str, text: str, position: str = "bottom_right", opacity: int = 128) -> str:
    """添加文字水印。

    Args:
        file_path: 图片文件路径
        text: 水印文字
        position: 水印位置，支持: top_left, top_right, bottom_left, bottom_right, center
        opacity: 透明度 0-255，默认 128
    """
    p = Path(file_path)
    if not p.exists():
        return f"错误：文件不存在 {file_path}"
    try:
        with Image.open(p) as img:
            if img.mode != "RGBA":
                img = img.convert("RGBA")
            overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
            draw = ImageDraw.Draw(overlay)

            font_size = max(16, img.size[0] // 20)
            try:
                font = ImageFont.truetype("/System/Library/Fonts/PingFang.ttc", font_size)
            except (OSError, IOError):
                try:
                    font = ImageFont.truetype("arial.ttf", font_size)
                except (OSError, IOError):
                    font = ImageFont.load_default()

            bbox = draw.textbbox((0, 0), text, font=font)
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
            w, h = img.size
            margin = 10

            positions = {
                "top_left": (margin, margin),
                "top_right": (w - tw - margin, margin),
                "bottom_left": (margin, h - th - margin),
                "bottom_right": (w - tw - margin, h - th - margin),
                "center": ((w - tw) // 2, (h - th) // 2),
            }
            pos = positions.get(position, positions["bottom_right"])
            draw.text(pos, text, fill=(255, 255, 255, opacity), font=font)

            out = Image.alpha_composite(img, overlay)
            out_path = _output_path(file_path, "watermark", "png")
            out.save(out_path)
            return _done(out_path)
    except Exception as e:
        return f"错误：{e}"


_FORMAT_MAP = {"jpg": "JPEG", "jpeg": "JPEG", "png": "PNG", "webp": "WEBP", "bmp": "BMP"}


@tool
def image_convert(file_path: str, target_format: str) -> str:
    """转换图片格式。

    Args:
        file_path: 图片文件路径
        target_format: 目标格式，支持: png, jpg, jpeg, webp, bmp
    """
    p = Path(file_path)
    if not p.exists():
        return f"错误：文件不存在 {file_path}"
    fmt = target_format.lower().strip()
    if fmt not in _FORMAT_MAP:
        return f"不支持的格式: {fmt}。支持: {', '.join(_FORMAT_MAP.keys())}"
    try:
        with Image.open(p) as img:
            if fmt in ("jpg", "jpeg") and img.mode in ("RGBA", "LA", "P"):
                img = img.convert("RGB")
            out_path = _output_path(file_path, "convert", fmt)
            img.save(out_path, _FORMAT_MAP[fmt])
            return _done(out_path)
    except Exception as e:
        return f"错误：{e}"
