"""Best-effort metadata extraction for locally synchronized Office files."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree
from zipfile import ZipFile

_CORE_PROPERTIES_PATH = "docProps/core.xml"
_NAMESPACES = {
    "cp": "http://schemas.openxmlformats.org/package/2006/metadata/core-properties",
    "dc": "http://purl.org/dc/elements/1.1/",
    "dcterms": "http://purl.org/dc/terms/",
}


@dataclass(frozen=True)
class DocumentMetadata:
    """Embedded document properties, not authoritative SharePoint metadata."""

    author: str | None = None
    created_at: str | None = None
    last_modified_by: str | None = None
    modified_at: str | None = None


def _clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


def _property_text(root: ElementTree.Element, name: str) -> str | None:
    element = root.find(name, _NAMESPACES)
    return _clean_text(element.text if element is not None else None)


def extract_document_metadata(file_path: str) -> DocumentMetadata:
    """Read embedded PPTX/DOCX core properties; PDFs are intentionally ignored."""
    extension = Path(file_path).suffix.lower()
    if extension not in {".pptx", ".docx"}:
        return DocumentMetadata()

    with ZipFile(file_path) as package:
        try:
            core_xml = package.read(_CORE_PROPERTIES_PATH)
        except KeyError:
            return DocumentMetadata()
    properties = ElementTree.fromstring(core_xml)

    return DocumentMetadata(
        author=_property_text(properties, "dc:creator"),
        created_at=_property_text(properties, "dcterms:created"),
        last_modified_by=_property_text(properties, "cp:lastModifiedBy"),
        modified_at=_property_text(properties, "dcterms:modified"),
    )
