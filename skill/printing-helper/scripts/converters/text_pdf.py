"""Plain text / Markdown / HTML to PDF via headless Chrome."""

from __future__ import annotations

import html as html_lib
from pathlib import Path

from converters import (chrome_print_html, html_document, page_count,
                        read_text_auto)


def _wrap_html_fragment(fragment: str) -> str:
    lowered = fragment.lstrip().lower()
    if lowered.startswith("<!doctype") or "<html" in lowered[:200]:
        return fragment
    return f"<div class=\"html-fragment\">{fragment}</div>"


def convert(path: Path, out_pdf: Path, *, paper: tuple[float, float],
            margins: tuple[float, float, float, float], orientation: str) -> dict:
    suffix = path.suffix.lower()
    text, encoding = read_text_auto(path)
    warnings: list[str] = []
    if encoding not in {"utf-8", "utf-8-sig"}:
        warnings.append(f"{path.name} 使用 {encoding} 解码")

    if suffix in {".html", ".htm"}:
        body = _wrap_html_fragment(text)
        doc = html_document(body, paper=paper, margins=margins,
                            orientation=orientation)
    elif suffix in {".md", ".markdown"}:
        import markdown
        fragment = markdown.markdown(
            text, extensions=["tables", "fenced_code", "sane_lists", "nl2br"])
        doc = html_document(fragment, paper=paper, margins=margins,
                            orientation=orientation)
    else:
        escaped = html_lib.escape(text)
        body = f"<pre class=\"plain\">{escaped}</pre>"
        doc = html_document(body, paper=paper, margins=margins,
                            orientation=orientation)

    chrome_print_html(doc, out_pdf)
    return {"pages": page_count(out_pdf), "method": "chrome", "warnings": warnings,
            "encoding": encoding}