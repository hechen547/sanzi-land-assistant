from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

from PIL import Image, ImageColor, ImageDraw, ImageFont, ImageOps

from ..models.photo import PhotoInfo
from .rename_service import unique_destination

ORIENTATION_TAG_ID = 274


@dataclass(slots=True)
class WatermarkConfig:
    enabled: bool = True
    title_enabled: bool = True
    title: str = "现场照片"
    latitude_enabled: bool = True
    longitude_enabled: bool = True
    time_enabled: bool = True
    custom_text: str = ""
    font_path: str = ""
    font_size: int = 48
    color: str = "#FFFFFF"
    stroke_color: str = "#000000"
    stroke_width: int = 2
    opacity: int = 255
    left_margin: int = 40
    bottom_margin: int = 40
    line_spacing: int = 8

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def build_watermark_lines(photo: PhotoInfo, config: WatermarkConfig) -> list[str]:
    lines: list[str] = []
    if config.title_enabled and config.title.strip():
        lines.append(config.title.strip())
    if config.latitude_enabled and photo.lat is not None:
        lines.append(f"纬度：{photo.lat:.6f}")
    if config.longitude_enabled and photo.lon is not None:
        lines.append(f"经度：{photo.lon:.6f}")
    if config.time_enabled and photo.shot_time:
        lines.append(f"时间：{photo.shot_time_text}")
    lines.extend(line.strip() for line in config.custom_text.splitlines() if line.strip())
    return lines


def apply_watermark(
    photo: PhotoInfo,
    output_dir: str | Path,
    config: WatermarkConfig,
    output_filename: str | None = None,
) -> Path:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = unique_destination(output_dir, output_filename or photo.filename)

    with Image.open(photo.full_path) as source:
        exif = source.getexif()
        image = render_watermarked_image(source, photo, config)
        # 像素已经按 EXIF 方向转正，保存旧方向会导致部分看图软件再次旋转。
        if ORIENTATION_TAG_ID in exif:
            exif[ORIENTATION_TAG_ID] = 1
        save_image = image.convert("RGB") if destination.suffix.lower() in {".jpg", ".jpeg"} else image
        save_kwargs: dict[str, object] = {}
        if exif:
            save_kwargs["exif"] = exif.tobytes()
        if destination.suffix.lower() in {".jpg", ".jpeg"}:
            save_kwargs.update(quality=95, subsampling=0)
        save_image.save(destination, **save_kwargs)
    return destination


def render_watermarked_image(
    source: Image.Image,
    photo: PhotoInfo,
    config: WatermarkConfig,
    max_size: tuple[int, int] | None = None,
) -> Image.Image:
    image = ImageOps.exif_transpose(source).convert("RGBA")
    if max_size:
        image.thumbnail(max_size, Image.Resampling.LANCZOS)
    if config.enabled:
        _draw_watermark(image, build_watermark_lines(photo, config), config)
    return image


def _draw_watermark(image: Image.Image, lines: list[str], config: WatermarkConfig) -> None:
    if not lines:
        return
    draw = ImageDraw.Draw(image)
    font = _load_font(config)
    fill = _with_opacity(config.color, config.opacity)
    stroke_fill = _with_opacity(config.stroke_color, config.opacity)
    line_heights = []
    for line in lines:
        box = draw.textbbox((0, 0), line, font=font, stroke_width=config.stroke_width)
        line_heights.append(box[3] - box[1])
    total_height = sum(line_heights) + config.line_spacing * (len(lines) - 1)
    y = max(0, image.height - config.bottom_margin - total_height)
    for line, height in zip(lines, line_heights):
        draw.text(
            (config.left_margin, y),
            line,
            font=font,
            fill=fill,
            stroke_width=config.stroke_width,
            stroke_fill=stroke_fill,
        )
        y += height + config.line_spacing


def _load_font(config: WatermarkConfig) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        config.font_path,
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return ImageFont.truetype(candidate, config.font_size)
    return ImageFont.load_default()


def _with_opacity(color: str, opacity: int) -> tuple[int, int, int, int]:
    red, green, blue = ImageColor.getrgb(color)
    return red, green, blue, max(0, min(255, opacity))
