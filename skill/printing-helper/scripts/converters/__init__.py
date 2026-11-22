"""Shared helpers for the printing-helper converters."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from errors import ComposeError, UsageError

MM_TO_PT = 72.0 / 25.4
PAPER_SIZES = {
    "A4": (595.276, 841.89),
    "A5": (419.528, 595.276),
    "A3": (841.89, 1190.55),
    "Letter": (612.0, 792.0),
    "Legal": (612.0, 1008.0),
}
MARGIN_PRESETS = {"normal": 25.4, "narrow": 12.7, "none": 0.0}

CJK_FONTS = ('"Noto Sans CJK SC", "Noto Sans CJK", "Source Han Sans SC", '
             '"AR PL UMing CN", "Droid Sans Fallback", sans-serif')
MONO_FONTS = ('"Noto Sans Mono CJK SC", "Noto Sans CJK SC", "DejaVu Sans Mono", '
              'monospace')


def mm(value: float) -> float:
    return float(value) * MM_TO_PT


def parse_paper(value: str | None) -> tuple[float, float]:
    if not value:
        return PAPER_SIZES["A4"]
    key = value.strip()
    for name, size in PAPER_SIZES.items():
        if key.casefold() == name.casefold():
            return size
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*[xX×]\s*(\d+(?:\.\d+)?)\s*(mm|cm|pt)?",
                         key)
    if match:
        w, h, unit = float(match.group(1)), float(match.group(2)), match.group(3)
        factor = {"mm": MM_TO_PT, "cm": MM_TO_PT * 10, "pt": 1.0, None: MM_TO_PT}[unit]
        return w * factor, h * factor
    raise UsageError(f"无法识别纸张: {value}(示例: A4 / Letter / 210x297mm)")


def parse_margins(value: str | None) -> tuple[tuple[float, float, float, float], str]:
    """Return ``((top, right, bottom), left)`` in points plus a mode label."""
    raw = (value or "normal").strip()
    preset = raw.casefold()
    if preset in MARGIN_PRESETS:
        pts = mm(MARGIN_PRESETS[preset])
        return (pts, pts, pts, pts), preset
    parts = [p.strip() for p in re.split(r"[,/ ]+", raw) if p.strip()]
    if len(parts) != 4:
        raise UsageError("自定义边距需要四边数值,例如 --margin 20,20,20,20 (单位 mm)")
    try:
        vals = [mm(float(p)) for p in parts]
    except ValueError as exc:
        raise UsageError("边距数值必须是数字(mm)") from exc
    return (vals[0], vals[1], vals[2], vals[3], ), "custom"


def orientation_size(paper: tuple[float, float], orientation: str) -> tuple[float, float]:
    width, height = paper
    if orientation == "landscape":
        return (max(width, height), min(width, height))
    if orientation == "portrait":
        return (min(width, height), max(width, height))
    raise UsageError(f"未知方向: {orientation}")


def content_box(paper: tuple[float, float], margins: tuple[float, float, float, float],
                orientation: str) -> tuple[float, float, float, float]:
    width, height = orientation_size(paper, orientation)
    top, right, bottom, left = margins
    x0, y0 = left, top
    x1, y1 = width - right, height - bottom
    if x1 <= x0 or y1 <= y0:
        raise ComposeError("边距过大,内容区域为空")
    return (x0, y0, x1, y1)


def find_chrome() -> str:
    for candidate in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        path = shutil.which(candidate)
        if path:
            return path
    raise ComposeError("未找到 Chrome/Chromium,无法渲染文本或 docx")


def run_cmd(cmd: list[str], timeout: float = 90, what: str = "命令") -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise ComposeError(f"{what}不可用: {cmd[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ComposeError(f"{what}超时({timeout:.0f}s): {' '.join(cmd[:3])}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise ComposeError(f"{what}失败 (exit {proc.returncode}): {detail[:500]}")
    return proc


def html_document(body: str, *, paper: tuple[float, float], margins: tuple[float, float, float, float],
                  orientation: str, extra_css: str = "") -> str:
    width, height = orientation_size(paper, orientation)
    top, right, bottom, left = margins
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><style>
@page {{ size: {width:.2f}pt {height:.2f}pt; margin: {top:.2f}pt {right:.2f}pt {bottom:.2f}pt {left:.2f}pt; }}
html, body {{ margin: 0; padding: 0; }}
body {{ font-family: {CJK_FONTS}; font-size: 11pt; line-height: 1.65; color: #111; }}
h1 {{ font-size: 20pt; margin: 0 0 12pt; }}
h2 {{ font-size: 15pt; margin: 16pt 0 8pt; }}
h3 {{ font-size: 12.5pt; margin: 12pt 0 6pt; }}
p {{ margin: 0 0 8pt; }}
ul, ol {{ margin: 0 0 8pt 20pt; padding: 0; }}
pre {{ white-space: pre-wrap; word-break: break-word; font-family: {MONO_FONTS}; font-size: 9.5pt; line-height: 1.45; margin: 0 0 8pt; }}
pre.plain {{ font-size: 10.5pt; line-height: 1.5; }}
code {{ font-family: {MONO_FONTS}; font-size: 9.5pt; }}
table {{ border-collapse: collapse; width: 100%; margin: 0 0 10pt; }}
th, td {{ border: 1px solid #888; padding: 4pt 6pt; text-align: left; vertical-align: top; }}
th {{ background: #f0f0f0; }}
img {{ max-width: 100%; height: auto; }}
blockquote {{ margin: 0 0 8pt 12pt; padding-left: 8pt; border-left: 3px solid #ccc; color: #444; }}
{extra_css}
</style></head><body>
{body}
</body></html>"""


def chrome_print_html(html: str, out_pdf: Path) -> None:
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    html_path = out_pdf.with_suffix(".html")
    html_path.write_text(html, "utf-8")
    chrome = find_chrome()
    cmd = [
        chrome, "--headless", "--disable-gpu", "--no-sandbox",
        "--no-pdf-header-footer", "--virtual-time-budget=5000",
        f"--print-to-pdf={out_pdf}", html_path.resolve().as_uri(),
    ]
    run_cmd(cmd, timeout=90, what="Chrome PDF 渲染")
    if not out_pdf.exists() or out_pdf.stat().st_size == 0:
        raise ComposeError(f"Chrome 未生成 PDF: {out_pdf}")


def read_text_auto(path: Path) -> tuple[str, str]:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "big5", "latin-1"):
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8(replace)"


def page_count(pdf_path: Path) -> int:
    import fitz
    with fitz.open(pdf_path) as doc:
        return doc.page_count


def ensure_parent(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}