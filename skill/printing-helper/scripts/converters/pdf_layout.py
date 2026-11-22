"""PDF page placement and margin handling with PyMuPDF."""

from __future__ import annotations

import fitz

from converters import content_box, orientation_size
from errors import ComposeError


def append_pdf(out: fitz.Document, src: fitz.Document, *, paper: tuple[float, float],
               margins: tuple[float, float, float, float], orientation: str,
               margin_mode: str) -> tuple[int, int]:
    """Append ``src`` into ``out``; returns (start_page_index, page_count).

    ``margin_mode == "none"`` copies pages unchanged.  Otherwise each page is
    scaled uniformly and centred inside the requested content box: vectors stay
    vectors, but the effective font size follows the scale factor.
    """
    if src.needs_pass:
        raise ComposeError("PDF 已加密,无法处理")
    start = out.page_count

    if margin_mode == "none":
        out.insert_pdf(src)
        return start, src.page_count

    for index in range(src.page_count):
        source_page = src[index]
        source_w, source_h = source_page.rect.width, source_page.rect.height
        orient = orientation
        if orientation == "auto":
            orient = "landscape" if source_w > source_h else "portrait"
        width, height = orientation_size(paper, orient)
        box = content_box(paper, margins, orient)
        page = out.new_page(width=width, height=height)
        page.show_pdf_page(fitz.Rect(box), src, index, keep_proportion=True)
    return start, src.page_count