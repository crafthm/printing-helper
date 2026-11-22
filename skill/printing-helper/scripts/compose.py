"""Turn input files into one print-ready PDF (the deterministic core)."""

from __future__ import annotations

import re
import shutil
import zipfile
from datetime import datetime
from pathlib import Path

import fitz
from PIL import Image

from converters import MM_TO_PT, page_count
from converters import docx_pdf, image_layout, pdf_layout, text_pdf
from errors import ComposeError, InputError
import jobs

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif"}
TEXT_EXTS = {
    ".txt", ".text", ".log", ".csv", ".md", ".markdown", ".html", ".htm",
    ".json", ".xml", ".yaml", ".yml", ".ini", ".conf", ".sh", ".py", ".css",
}
PDF_EXTS = {".pdf"}
DOCX_EXTS = {".docx"}
UNSUPPORTED_WORD_EXTS = {".doc", ".rtf", ".odt"}


def detect_kind(path: Path) -> str:
    if not path.exists():
        raise InputError(f"文件不存在: {path}")
    if not path.is_file():
        raise InputError(f"不是文件: {path}")
    if path.stat().st_size == 0:
        raise InputError(f"文件为空: {path}")

    suffix = path.suffix.lower()
    if suffix in IMAGE_EXTS:
        return "image"
    if suffix in PDF_EXTS:
        return "pdf"
    if suffix in DOCX_EXTS:
        return "docx"
    if suffix in UNSUPPORTED_WORD_EXTS:
        raise InputError(
            f"{path.name}: 暂不支持 {suffix}(本机未安装 LibreOffice Writer)。"
            "请另存为 docx 或 PDF 后重试。")
    if suffix in TEXT_EXTS:
        return "text"

    header = path.read_bytes()[:8]
    if header.startswith(b"%PDF"):
        return "pdf"
    if header.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(path) as archive:
                if "word/document.xml" in archive.namelist():
                    return "docx"
        except zipfile.BadZipFile:
            pass
    try:
        with Image.open(path) as img:
            img.verify()
        return "image"
    except Exception:
        pass
    return "text"


def parse_grid(value: str | None) -> tuple[int, int] | None:
    if not value or value.strip().lower() == "auto":
        return None
    match = re.fullmatch(r"(\d+)\s*[x×*]\s*(\d+)", value.strip())
    if not match:
        raise InputError("--grid 需要 RxC 格式,例如 2x2;或使用 auto")
    cols, rows = int(match.group(1)), int(match.group(2))
    if cols < 1 or rows < 1:
        raise InputError("--grid 的行列数必须大于 0")
    return cols, rows


def _add_page_numbers(doc: fitz.Document) -> None:
    total = doc.page_count
    for index, page in enumerate(doc):
        text = f"{index + 1}/{total}"
        size = 8
        width = fitz.get_text_length(text, fontname="helv", fontsize=size)
        page.insert_text(
            ((page.rect.width - width) / 2.0, page.rect.height - 18),
            text, fontname="helv", fontsize=size, color=(0.35, 0.35, 0.35))


def build_job(inputs: list[str], *, options: dict, printer: dict,
              job_id: str | None = None) -> dict:
    """Compose all inputs into ``out.pdf`` and persist a JobSpec.

    No network or printer access happens here.  ``options`` must already be
    resolved (paper tuple, margins in points, etc.).
    """
    if not inputs:
        raise InputError("没有输入文件")

    paths = [Path(item).expanduser() for item in inputs]
    entries = [(path, detect_kind(path)) for path in paths]

    job_id = job_id or jobs.new_job_id()
    directory = jobs.job_dir(job_id)
    workdir = directory / "work"
    workdir.mkdir(parents=True, exist_ok=True)
    pdf_path = directory / "out.pdf"
    preview_path = directory / "preview-1.png"

    paper = options["paper"]
    margins = options["margins"]
    margin_mode = options["margin_mode"]
    orient = options["orient"]
    doc_orient = "portrait" if orient == "auto" else orient
    gap = options.get("gap_mm", 5.0) * MM_TO_PT

    outputs: list[dict] = []
    warnings: list[str] = []
    toc: list[list] = []
    fidelity = "high"
    out = fitz.open()

    try:
        index = 0
        while index < len(entries):
            path, kind = entries[index]

            if kind == "image":
                group = [path]
                cursor = index + 1
                while cursor < len(entries) and entries[cursor][1] == "image":
                    group.append(entries[cursor][0])
                    cursor += 1
                layout = options["images"]
                grid = parse_grid(options.get("grid"))
                info = image_layout.append_images(
                    out, group, paper=paper, margins=margins,
                    orientation=orient, layout=layout, grid=grid, gap=gap,
                    fill=bool(options.get("fill")), caption=bool(options.get("caption")),
                    workdir=workdir)
                label = group[0].name if len(group) == 1 else f"{len(group)} 张图片"
                toc.append([1, label, info["start"] + 1])
                outputs.append({
                    "input": [str(item) for item in group],
                    "kind": "image", "count": len(group),
                    "layout": layout, "grid": info.get("grid", "1x1"),
                    "orientation": info.get("orientation", orient),
                    "pages": info["pages"],
                    "method": "pillow+fitZ",
                })
                index = cursor
                continue

            start = out.page_count
            if kind == "pdf":
                with fitz.open(path) as source:
                    _, pages = pdf_layout.append_pdf(
                        out, source, paper=paper, margins=margins,
                        orientation=orient, margin_mode=margin_mode)
                method = "pdf-passthrough" if margin_mode == "none" else "pdf-scale"
                outputs.append({"input": str(path), "kind": "pdf", "pages": pages,
                                "method": method})
                if margin_mode != "none":
                    warnings.append(
                        f"{path.name}: PDF 采用等比缩放适配边距(字号随比例变化)")
            elif kind == "text":
                target = workdir / f"text-{index}.pdf"
                info = text_pdf.convert(path, target, paper=paper, margins=margins,
                                        orientation=doc_orient)
                with fitz.open(target) as source:
                    out.insert_pdf(source)
                outputs.append({"input": str(path), "kind": "text",
                                "pages": info["pages"], "method": info["method"]})
                warnings.extend(info.get("warnings", []))
            elif kind == "docx":
                target = workdir / f"docx-{index}.pdf"
                info = docx_pdf.convert(path, target, paper=paper, margins=margins,
                                        orientation=doc_orient)
                with fitz.open(target) as source:
                    out.insert_pdf(source)
                fidelity = "reduced"
                outputs.append({"input": str(path), "kind": "docx",
                                "pages": info["pages"], "method": info["method"],
                                "fidelity": "reduced"})
                warnings.extend(info.get("warnings", []))
                warnings.append(f"{path.name}: docx 经 Mammoth 渲染,页眉/页脚与精确分页可能简化")
            else:
                raise InputError(f"不支持的类型: {kind}")

            toc.append([1, path.name, start + 1])
            index += 1

        if out.page_count == 0:
            raise ComposeError("没有可打印内容")

        if options.get("page_numbers"):
            _add_page_numbers(out)
        if options.get("bookmarks", True) and toc:
            out.set_toc(toc)

        out.save(pdf_path, garbage=4, deflate=True)
    except Exception:
        out.close()
        raise
    else:
        out.close()

    with fitz.open(pdf_path) as doc:
        pix = doc[0].get_pixmap(dpi=110)
        pix.save(preview_path)

    printer_summary = {
        "slot": options.get("printer_slot"),
        "queue": printer.get("queue"),
        "host": printer.get("host"),
        "port": printer.get("port"),
        "transport": printer.get("transport"),
        "raw_format": options.get("raw_format") or printer.get("raw_format"),
        "pjl": bool(options.get("pjl", printer.get("pjl", False))),
        "paper": options.get("paper_name"),
    }

    spec = {
        "job_id": job_id,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "title": options.get("title") or paths[0].name,
        "printer": printer_summary,
        "paper": {"name": options.get("paper_name"), "size_pt": list(paper)},
        "margin": {"mode": margin_mode,
                   "pt": {"top": margins[0], "right": margins[1],
                          "bottom": margins[2], "left": margins[3]}},
        "orientation": orient,
        "copies": int(options.get("copies", 1)),
        "duplex": bool(options.get("duplex", False)),
        "raw_format": printer_summary["raw_format"],
        "pjl": printer_summary["pjl"],
        "outputs": outputs,
        "estimated_pages": page_count(pdf_path),
        "fidelity": fidelity,
        "warnings": warnings,
        "pdf_path": str(pdf_path),
        "preview_png": str(preview_path),
    }
    jobs.save_spec(spec)
    shutil.rmtree(workdir, ignore_errors=True)
    return spec