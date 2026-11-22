"""DOCX to PDF with Mammoth + Chrome (reduced fidelity, no LibreOffice needed).

The OOXML margin rewrite + LibreOffice path from the design is intentionally
not the default here: this machine has no ``libreoffice-writer``.  When Writer
is available the caller may opt into it later; Mammoth keeps the skill usable
with zero sudo.
"""

from __future__ import annotations

from pathlib import Path

from converters import chrome_print_html, html_document, page_count
from errors import ComposeError

DOCX_CSS = """
p { margin: 0 0 8pt; }
table { border-collapse: collapse; width: 100%; margin: 0 0 10pt; }
th, td { border: 1px solid #888; padding: 4pt 6pt; }
img { max-width: 100%; height: auto; }
"""


def available() -> bool:
    import importlib.util
    return importlib.util.find_spec("mammoth") is not None


def convert(path: Path, out_pdf: Path, *, paper: tuple[float, float],
            margins: tuple[float, float, float, float], orientation: str) -> dict:
    try:
        import mammoth
    except ImportError as exc:
        raise ComposeError(
            "未安装 mammoth,无法转换 docx。执行: python3 -m pip install mammoth"
        ) from exc

    with path.open("rb") as handle:
        result = mammoth.convert_to_html(handle)

    warnings = [str(message) for message in result.messages]
    body = result.value or "<p>(空文档)</p>"
    doc = html_document(body, paper=paper, margins=margins,
                        orientation=orientation, extra_css=DOCX_CSS)
    chrome_print_html(doc, out_pdf)
    return {
        "pages": page_count(out_pdf),
        "method": "mammoth+chrome",
        "fidelity": "reduced",
        "warnings": warnings,
    }