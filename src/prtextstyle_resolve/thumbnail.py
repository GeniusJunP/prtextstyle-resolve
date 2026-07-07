"""
thumbnail.py -- render a 103x58 RGBA PNG thumbnail as a sibling of a
Fusion Text+ `.setting` file, for the DaVinci Resolve Edit-page Titles
browser.

This module uses Pillow to render an accurate approximation of the Text+
preset, including shadows, strokes, and gradients, by consuming the parsed
`TextStyle` object directly.
"""

from __future__ import annotations

import math
import re
import shutil
import subprocess
import sys
import warnings
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .text_style import TextStyle

PathLike = Union[str, "Path"]

# Confirmed dimensions from reading PNG IHDR chunks of real FOG bundle thumbnails
THUMB_WIDTH = 103
THUMB_HEIGHT = 58

_START_PT = 100
_MIN_PT = 10
_MAX_TEXT_WIDTH = THUMB_WIDTH - 24
_MAX_TEXT_HEIGHT = THUMB_HEIGHT - 24

_FONT_DIRS = [
    Path.home() / "Library" / "Fonts",
    Path("/System/Library/Fonts"),
    Path("/Library/Fonts"),
]

_FC_LIST = shutil.which("fc-list")

# Cache of font-name -> resolved file path
_font_path_cache: Dict[str, Optional[str]] = {}


def _normalize_font_key(s: str) -> str:
    return re.sub(r"[\s\-_]+", "", s).lower()


def _resolve_via_fc_list(font_name: str) -> Optional[str]:
    if not _FC_LIST:
        return None
    for query in (f":postscriptname={font_name}", font_name):
        try:
            result = subprocess.run(
                [_FC_LIST, query, "--format=%{file}\n"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        for line in result.stdout.splitlines():
            candidate = line.strip()
            if candidate and Path(candidate).exists():
                return candidate
    return None


def _iter_candidate_font_files() -> List[Path]:
    candidates: List[Path] = []
    for d in _FONT_DIRS:
        if not d.is_dir():
            continue
        for ext in ("*.otf", "*.ttf", "*.ttc"):
            try:
                candidates.extend(d.rglob(ext))
            except OSError:
                continue
    return candidates


def _postscript_name(path: Path) -> Optional[str]:
    try:
        from fontTools.ttLib import TTCollection, TTFont  # type: ignore
    except ImportError:
        return None
    try:
        if path.suffix.lower() == ".ttc":
            collection = TTCollection(str(path), lazy=True)
            if not collection.fonts:
                return None
            font = collection.fonts[0]
        else:
            font = TTFont(str(path), lazy=True, fontNumber=0)
        name_table = font["name"]
        return name_table.getDebugName(6)
    except Exception:
        return None


def _resolve_via_directory_scan(font_name: str) -> Optional[str]:
    candidates = _iter_candidate_font_files()
    for path in candidates:
        ps_name = _postscript_name(path)
        if ps_name and ps_name.lower() == font_name.lower():
            return str(path)
    target = _normalize_font_key(font_name)
    for path in candidates:
        if _normalize_font_key(path.stem) == target:
            return str(path)
    return None


def resolve_font_path(font_name: str) -> Tuple[Optional[str], str]:
    if font_name in _font_path_cache:
        cached = _font_path_cache[font_name]
        return cached, ("resolved" if cached else "unresolved")

    path = _resolve_via_fc_list(font_name)
    method = "fc-list"
    if not path:
        path = _resolve_via_directory_scan(font_name)
        method = "directory-scan"
    if not path:
        method = "unresolved"

    _font_path_cache[font_name] = path
    return path, method


def _get_font(
    path: Optional[str], pt: int
) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    if path:
        try:
            return ImageFont.truetype(path, pt)
        except OSError:
            pass
    return ImageFont.load_default()


def _fit_font(
    draw: ImageDraw.ImageDraw, text: str, font_path: Optional[str]
) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    pt = _START_PT
    while pt > _MIN_PT:
        font = _get_font(font_path, pt)
        bbox = draw.textbbox((0, 0), text, font=font)
        w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        if w <= _MAX_TEXT_WIDTH and h <= _MAX_TEXT_HEIGHT:
            return font
        pt -= 2
    return _get_font(font_path, _MIN_PT)


def _draw_gradient_mask(w: int, h: int, stops, angle_deg: float) -> Image.Image:
    """Generate a gradient image to be used as a source pattern."""
    img = Image.new("RGBA", (w, h))
    pixels = img.load()
    if not pixels:
        return img

    angle_rad = math.radians(angle_deg - 90.0)
    cos_a = math.cos(angle_rad)
    sin_a = math.sin(angle_rad)

    # Calculate projection range to normalize stops
    cx, cy = w / 2.0, h / 2.0
    corners = [(0, 0), (w, 0), (0, h), (w, h)]
    projs = [(x - cx) * cos_a + (y - cy) * sin_a for x, y in corners]
    min_p, max_p = min(projs), max(projs)
    length = max_p - min_p if max_p != min_p else 1.0

    for y in range(h):
        for x in range(w):
            p = (x - cx) * cos_a + (y - cy) * sin_a
            pos = (p - min_p) / length

            # Find stops
            s1, s2 = stops[0], stops[-1]
            for i in range(len(stops) - 1):
                if stops[i].position <= pos <= stops[i + 1].position:
                    s1, s2 = stops[i], stops[i + 1]
                    break

            # Interpolate
            span = s2.position - s1.position
            if span <= 0:
                frac = 0.0
            else:
                frac = (pos - s1.position) / span

            r = int(s1.r + (s2.r - s1.r) * frac)
            g = int(s1.g + (s2.g - s1.g) * frac)
            b = int(s1.b + (s2.b - s1.b) * frac)
            a = int((s1.opacity + (s2.opacity - s1.opacity) * frac) * 255)
            pixels[x, y] = (r, g, b, a)

    return img


def generate_thumbnail(style: TextStyle, out_path: PathLike) -> Path:
    """Generate thumbnail image from TextStyle object"""
    resolved_out = Path(out_path)
    text = "Ag"

    font_path: Optional[str] = None
    if style.font:
        font_name_query = style.font
        if style.font_style:
            font_name_query += f":style={style.font_style}"
        font_path, _ = resolve_font_path(font_name_query)
        if not font_path:
            # fallback to exact PS name just in case
            font_path, _ = resolve_font_path(style.font_ps_name or "")

    img = Image.new("RGBA", (THUMB_WIDTH, THUMB_HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    font = _fit_font(draw, text, font_path)

    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]

    x = (THUMB_WIDTH - tw) / 2 - bbox[0]
    y = (THUMB_HEIGHT - th) / 2 - bbox[1]

    # scale: font render size relative to Premiere's nominal size_pt
    scale = font.size / style.size_pt if style.size_pt > 0 else 1.0

    # 1. Shadows
    for shadow in reversed(style.shadows):
        rgba = (shadow.r, shadow.g, shadow.b, int((shadow.opacity_pct / 100.0) * 255))
        stroke_px = int(shadow.size_px * scale) if shadow.size_px > 0 else 0

        # Premiere angle: 0=right, 90=down (clockwise). PIL Y is downward.
        # X axis in Premiere is: angle=0 → shadow goes RIGHT.
        # cos(0)=+1 → but empirically shadow appears on OPPOSITE side of angle.
        # User confirmed: angle≈162° → shadow should appear to the right (+x).
        # cos(162°) < 0, so we negate to get +x → correct.
        offset_x = (
            -shadow.distance_px * math.cos(math.radians(shadow.angle_deg)) * scale
        )
        offset_y = shadow.distance_px * math.sin(math.radians(shadow.angle_deg)) * scale

        shadow_img = Image.new("RGBA", (THUMB_WIDTH, THUMB_HEIGHT), (0, 0, 0, 0))
        shadow_draw = ImageDraw.Draw(shadow_img)

        if shadow.use_gradient and shadow.gradient_stops:
            grad_img = _draw_gradient_mask(
                THUMB_WIDTH,
                THUMB_HEIGHT,
                shadow.gradient_stops,
                shadow.gradient_angle_deg or 0,
            )
            mask_img = Image.new("L", (THUMB_WIDTH, THUMB_HEIGHT), 0)
            mask_draw = ImageDraw.Draw(mask_img)
            mask_draw.text(
                (x + offset_x, y - offset_y),
                text,
                font=font,
                fill=255,
                stroke_width=stroke_px,
                stroke_fill=255,
            )

            comp_img = Image.new("RGBA", (THUMB_WIDTH, THUMB_HEIGHT), (0, 0, 0, 0))
            comp_img.paste(grad_img, (0, 0), mask_img)
            shadow_img.alpha_composite(comp_img)
        else:
            shadow_draw.text(
                (x + offset_x, y - offset_y),
                text,
                font=font,
                fill=rgba,
                stroke_width=stroke_px,
                stroke_fill=rgba,
            )

        blur_radius = (shadow.blur_px * scale) / 8.0
        if blur_radius > 0:
            shadow_img = shadow_img.filter(ImageFilter.GaussianBlur(blur_radius))

        img.alpha_composite(shadow_img)

    # 2. Strokes
    for stroke in reversed(style.strokes):
        rgba = (stroke.r, stroke.g, stroke.b, 255)
        stroke_px = int(stroke.width_px * scale)

        if bool(stroke.gradient_stops):
            grad_img = _draw_gradient_mask(
                THUMB_WIDTH,
                THUMB_HEIGHT,
                stroke.gradient_stops,
                stroke.gradient_angle_deg or 0,
            )
            mask_img = Image.new("L", (THUMB_WIDTH, THUMB_HEIGHT), 0)
            mask_draw = ImageDraw.Draw(mask_img)
            mask_draw.text(
                (x, y),
                text,
                font=font,
                fill=255,
                stroke_width=stroke_px,
                stroke_fill=255,
            )

            comp_img = Image.new("RGBA", (THUMB_WIDTH, THUMB_HEIGHT), (0, 0, 0, 0))
            comp_img.paste(grad_img, (0, 0), mask_img)
            img.alpha_composite(comp_img)
        else:
            draw.text(
                (x, y),
                text,
                font=font,
                fill=rgba,
                stroke_width=stroke_px,
                stroke_fill=rgba,
            )

    # 3. Fill
    if style.fill.mode == "gradient" and style.fill.gradient_stops:
        grad_img = _draw_gradient_mask(
            THUMB_WIDTH,
            THUMB_HEIGHT,
            style.fill.gradient_stops,
            style.fill.gradient_angle_deg or 0,
        )
        mask_img = Image.new("L", (THUMB_WIDTH, THUMB_HEIGHT), 0)
        mask_draw = ImageDraw.Draw(mask_img)
        mask_draw.text((x, y), text, font=font, fill=255)

        comp_img = Image.new("RGBA", (THUMB_WIDTH, THUMB_HEIGHT), (0, 0, 0, 0))
        comp_img.paste(grad_img, (0, 0), mask_img)
        img.alpha_composite(comp_img)
    else:
        rgba = (style.fill.r, style.fill.g, style.fill.b, int(style.fill.alpha * 255))
        draw.text((x, y), text, font=font, fill=rgba)

    resolved_out.parent.mkdir(parents=True, exist_ok=True)
    img.save(resolved_out, format="PNG")
    return resolved_out
