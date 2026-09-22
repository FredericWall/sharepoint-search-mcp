"""Extrahiert Text aus PPTX-, DOCX- und PDF-Dateien.

Jede Extraktions-Funktion liefert eine Liste von (page_or_slide, text)-Tupeln,
wobei page_or_slide 1-basiert ist.
"""

import os

from docx import Document
from pptx import Presentation


class ExtractionError(Exception):
    """Wird geworfen, wenn ein Dateityp nicht unterstützt wird oder Extraktion fehlschlägt."""


def extract_text(file_path: str) -> list[tuple[int, str]]:
    """Extrahiert Text aus einer Datei anhand ihrer Endung.

    Returns:
        Liste von (page_or_slide, text). Leere Seiten werden übersprungen.

    Raises:
        ExtractionError: bei nicht unterstütztem Dateityp.
    """
    ext = os.path.splitext(file_path)[1].lower()
    if ext == ".pptx":
        return _extract_pptx(file_path)
    if ext == ".docx":
        return _extract_docx(file_path)
    if ext == ".pdf":
        return _extract_pdf(file_path)
    raise ExtractionError(f"Nicht unterstützter Dateityp: {ext}")


def _extract_pptx(file_path: str) -> list[tuple[int, str]]:
    prs = Presentation(file_path)
    out: list[tuple[int, str]] = []
    for idx, slide in enumerate(prs.slides, start=1):
        parts: list[str] = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                parts.append(shape.text_frame.text)
        text = "\n".join(p for p in parts if p.strip())
        if text.strip():
            out.append((idx, text))
    return out


def _extract_docx(file_path: str) -> list[tuple[int, str]]:
    doc = Document(file_path)
    text = "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    return [(1, text)] if text.strip() else []


def _extract_pdf(file_path: str) -> list[tuple[int, str]]:
    import pdfplumber

    out: list[tuple[int, str]] = []
    with pdfplumber.open(file_path) as pdf:
        for idx, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            if text.strip():
                out.append((idx, text))
    return out
