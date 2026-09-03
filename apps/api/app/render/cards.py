"""Title and end cards, rendered to PNG with Pillow.

This ffmpeg has no `drawtext` (no libfreetype), but rendering text in Python is
the better design regardless: a card becomes an ordinary still clip, so it
needs no special stage, inherits the same normalisation, and can be inspected
as an image before a render.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

#: Checked in order. A real typeface matters -- the bitmap fallback looks like
#: a placeholder, which is exactly what it is.
_FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Georgia.ttf",
    "/System/Library/Fonts/Supplemental/Times New Roman.ttf",
    "/System/Library/Fonts/Palatino.ttc",
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "C:/Windows/Fonts/georgia.ttf",
]


@lru_cache(maxsize=8)
def _font(size: int) -> ImageFont.FreeTypeFont:
    for path in _FONT_CANDIDATES:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:               # Pillow < 10.1
        return ImageFont.load_default()


def available_font() -> str:
    for path in _FONT_CANDIDATES:
        if Path(path).exists():
            return path
    return "PIL default (bitmap)"


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_w: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if draw.textlength(trial, font=font) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def render_card(
    text: str, dest: Path, *, width: int, height: int, subtitle: str = "",
    bg: tuple[int, int, int] = (12, 12, 14),
    fg: tuple[int, int, int] = (242, 238, 230),
) -> Path:
    """Draw a centred title card and write it as a PNG."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (width, height), bg)
    draw = ImageDraw.Draw(img)

    title_font = _font(max(28, height // 12))
    sub_font = _font(max(18, height // 30))
    max_w = int(width * 0.78)

    lines = _wrap(draw, text, title_font, max_w)
    line_h = int(title_font.size * 1.28)
    sub_lines = _wrap(draw, subtitle, sub_font, max_w) if subtitle else []
    sub_h = int(sub_font.size * 1.4)

    block_h = len(lines) * line_h + (len(sub_lines) * sub_h + line_h // 2
                                     if sub_lines else 0)
    y = (height - block_h) // 2

    for ln in lines:
        w = draw.textlength(ln, font=title_font)
        draw.text(((width - w) / 2, y), ln, font=title_font, fill=fg)
        y += line_h
    if sub_lines:
        y += line_h // 2
        muted = tuple(int(c * 0.62) for c in fg)
        for ln in sub_lines:
            w = draw.textlength(ln, font=sub_font)
            draw.text(((width - w) / 2, y), ln, font=sub_font, fill=muted)
            y += sub_h

    img.save(dest, "PNG")
    return dest
