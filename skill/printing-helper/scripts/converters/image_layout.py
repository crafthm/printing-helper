"""Image placement: one image per page, or N images per sheet (N-up)."""

from __future__ import annotations

import math
import secrets
from pathlib import Path

import fitz
from PIL import Image, ImageOps

from converters import content_box, orientation_size
from errors import ComposeError

TARGET_DPI = 300
CAPTION_PT = 12.0


def image_size(path: Path) -> tuple[int, int]:
    try:
        with Image.open(path) as img:
            img = ImageOps.exif_transpose(img)
            return img.size
    except Exception as exc:
        raise ComposeError(f"无法读取图片 {path}: {exc}") from exc


def _load_rgb(path: Path) -> Image.Image:
    try:
        with Image.open(path) as raw:
            img = ImageOps.exif_transpose(raw)
            if img.mode in {"RGBA", "LA"} or (
                    img.mode == "P" and "transparency" in img.info):
                rgba = img.convert("RGBA")
                background = Image.new("RGB", rgba.size, "white")
                background.paste(rgba, mask=rgba.split()[-1])
                return background
            return img.convert("RGB").copy()
    except Exception as exc:
        raise ComposeError(f"无法读取图片 {path}: {exc}") from exc


def _prepare(path: Path, max_w_pt: float, max_h_pt: float,
             workdir: Path) -> tuple[Path, int, int]:
    img = _load_rgb(path)
    max_w = max(1, int(max_w_pt / 72.0 * TARGET_DPI))
    max_h = max(1, int(max_h_pt / 72.0 * TARGET_DPI))
    if img.width > max_w or img.height > max_h:
        img.thumbnail((max_w, max_h), Image.LANCZOS)
    return _save(img, workdir), img.width, img.height


def _cover(path: Path, aspect: float, max_w_pt: float, max_h_pt: float,
           workdir: Path) -> Path:
    img = _load_rgb(path)
    width, height = img.size
    current = width / height if height else 1.0
    if current > aspect:
        new_width = max(1, int(height * aspect))
        left = (width - new_width) // 2
        img = img.crop((left, 0, left + new_width, height))
    elif current < aspect:
        new_height = max(1, int(width / aspect))
        top = (height - new_height) // 2
        img = img.crop((0, top, width, top + new_height))
    max_w = max(1, int(max_w_pt / 72.0 * TARGET_DPI))
    max_h = max(1, int(max_h_pt / 72.0 * TARGET_DPI))
    if img.width > max_w or img.height > max_h:
        img.thumbnail((max_w, max_h), Image.LANCZOS)
    return _save(img, workdir)


def _save(img: Image.Image, workdir: Path) -> Path:
    workdir.mkdir(parents=True, exist_ok=True)
    out = workdir / f"img-{secrets.token_hex(4)}.jpg"
    img.save(out, "JPEG", quality=90, optimize=True)
    return out


def _fit_rect(box: tuple[float, float, float, float], iw: int, ih: int) -> fitz.Rect:
    x0, y0, x1, y1 = box
    cw, ch = x1 - x0, y1 - y0
    scale = min(cw / iw, ch / ih) if iw and ih else 1.0
    w, h = iw * scale, ih * scale
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    return fitz.Rect(cx - w / 2.0, cy - h / 2.0, cx + w / 2.0, cy + h / 2.0)


def auto_grid(count: int, paper: tuple[float, float],
              margins: tuple[float, float, float, float], gap: float,
              aspects: list[float]) -> tuple[int, int, str]:
    """Pick (cols, rows, orientation) that wastes least for the median image."""
    median = sorted(aspects)[len(aspects) // 2] if aspects else 1.0
    best: tuple[float, int, int, str] | None = None
    for orient in ("portrait", "landscape"):
        width, height = orientation_size(paper, orient)
        top, right, bottom, left = margins
        content_w = width - left - right
        content_h = height - top - bottom
        for rows in range(1, count + 1):
            cols = math.ceil(count / rows)
            cell_w = (content_w - gap * (cols - 1)) / cols
            cell_h = (content_h - gap * (rows - 1)) / rows
            if cell_w <= 0 or cell_h <= 0:
                continue
            cell_aspect = cell_w / cell_h
            blank = cols * rows - count
            score = abs(math.log(cell_aspect / median)) + blank * 0.35
            if best is None or score < best[0]:
                best = (score, cols, rows, orient)
    if best is None:
        return (1, count, "portrait")
    return best[1], best[2], best[3]


def _cell_aspect(cols: int, rows: int, paper: tuple[float, float],
                 margins: tuple[float, float, float, float], gap: float,
                 orient: str) -> float:
    width, height = orientation_size(paper, orient)
    content_w = width - margins[1] - margins[3]
    content_h = height - margins[0] - margins[2]
    cell_w = (content_w - gap * (cols - 1)) / cols
    cell_h = (content_h - gap * (rows - 1)) / rows
    return cell_w / cell_h if cell_h else 1.0


def _best_orientation(cols: int, rows: int, paper: tuple[float, float],
                      margins: tuple[float, float, float, float], gap: float,
                      aspects: list[float]) -> str:
    median = sorted(aspects)[len(aspects) // 2] if aspects else 1.0
    return min(("portrait", "landscape"),
               key=lambda orient: abs(math.log(
                   _cell_aspect(cols, rows, paper, margins, gap, orient) / median)))


def _caption_font(text: str) -> str:
    return "china-s" if any(ord(char) > 127 for char in text) else "helv"


def append_images(out: fitz.Document, paths: list[Path], *, paper: tuple[float, float],
                  margins: tuple[float, float, float, float], orientation: str,
                  layout: str, grid: tuple[int, int] | None, gap: float,
                  fill: bool, caption: bool, workdir: Path) -> dict:
    if layout == "one-per-page":
        return _one_per_page(out, paths, paper=paper, margins=margins,
                             orientation=orientation, fill=fill, workdir=workdir)
    if layout == "nup":
        return _nup(out, paths, paper=paper, margins=margins,
                    orientation=orientation, grid=grid, gap=gap,
                    fill=fill, caption=caption, workdir=workdir)
    raise ComposeError(f"未知图片排版: {layout}")


def _one_per_page(out: fitz.Document, paths: list[Path], *, paper,
                  margins, orientation: str, fill: bool, workdir: Path) -> dict:
    start = out.page_count
    for index, path in enumerate(paths):
        iw, ih = image_size(path)
        orient = orientation
        if orient == "auto":
            orient = "landscape" if iw > ih else "portrait"
        width, height = orientation_size(paper, orient)
        box = content_box(paper, margins, orient)
        page = out.new_page(width=width, height=height)
        box_w, box_h = box[2] - box[0], box[3] - box[1]
        if fill:
            prepared = _cover(path, box_w / box_h, box_w, box_h, workdir)
            rect = fitz.Rect(box)
        else:
            prepared, pw, ph = _prepare(path, box_w, box_h, workdir)
            rect = _fit_rect(box, pw, ph)
        page.insert_image(rect, filename=str(prepared))
    return {"start": start, "pages": len(paths), "layout": "one-per-page",
            "orientation": orientation}


def _nup(out: fitz.Document, paths: list[Path], *, paper, margins,
         orientation: str, grid: tuple[int, int] | None, gap: float,
         fill: bool, caption: bool, workdir: Path) -> dict:
    aspects = []
    for path in paths:
        iw, ih = image_size(path)
        aspects.append(iw / ih if ih else 1.0)

    if grid:
        cols, rows = grid
        orient = orientation
        if orient == "auto":
            orient = _best_orientation(cols, rows, paper, margins, gap, aspects)
    else:
        cols, rows, orient = auto_grid(len(paths), paper, margins, gap, aspects)

    width, height = orientation_size(paper, orient)
    box = content_box(paper, margins, orient)
    box_w, box_h = box[2] - box[0], box[3] - box[1]
    cell_w = (box_w - gap * (cols - 1)) / cols
    cell_h = (box_h - gap * (rows - 1)) / rows
    start = out.page_count
    per_page = cols * rows
    pages = math.ceil(len(paths) / per_page)

    for page_index in range(pages):
        page = out.new_page(width=width, height=height)
        for slot in range(per_page):
            index = page_index * per_page + slot
            if index >= len(paths):
                break
            row, col = divmod(slot, cols)
            x0 = box[0] + col * (cell_w + gap)
            y0 = box[1] + row * (cell_h + gap)
            cell = (x0, y0, x0 + cell_w, y0 + cell_h)
            image_bottom = cell[3] - (CAPTION_PT if caption else 0)
            image_box = (cell[0], cell[1], cell[2], image_bottom)
            path = paths[index]
            if fill:
                prepared = _cover(path, cell_w / (image_bottom - cell[1]),
                                  cell_w, image_bottom - cell[1], workdir)
                rect = fitz.Rect(image_box)
            else:
                prepared, pw, ph = _prepare(path, cell_w, image_bottom - cell[1], workdir)
                rect = _fit_rect(image_box, pw, ph)
            page.insert_image(rect, filename=str(prepared))
            if caption:
                text = f"{index + 1}. {path.name}"
                font = _caption_font(text)
                text_width = fitz.get_text_length(text, fontname=font, fontsize=7)
                page.insert_text(
                    (cell[0] + max(0.0, (cell_w - text_width) / 2.0), cell[3] - 3),
                    text, fontname=font, fontsize=7, color=(0.35, 0.35, 0.35))
    return {"start": start, "pages": pages, "layout": "nup",
            "grid": f"{cols}x{rows}", "orientation": orient}