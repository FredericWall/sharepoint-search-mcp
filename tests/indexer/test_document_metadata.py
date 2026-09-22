from datetime import datetime

from docx import Document
from pptx import Presentation

from indexer.document_metadata import DocumentMetadata, extract_document_metadata


def test_extracts_pptx_core_properties(tmp_path):
    path = tmp_path / "deck.pptx"
    presentation = Presentation()
    presentation.core_properties.author = "  Ada Lovelace  "
    presentation.core_properties.last_modified_by = "Grace Hopper"
    presentation.core_properties.created = datetime(2025, 1, 2, 3, 4, 5)
    presentation.core_properties.modified = datetime(2026, 2, 3, 4, 5, 6)
    presentation.save(path)

    metadata = extract_document_metadata(str(path))

    assert metadata == DocumentMetadata(
        author="Ada Lovelace",
        created_at="2025-01-02T03:04:05Z",
        last_modified_by="Grace Hopper",
        modified_at="2026-02-03T04:05:06Z",
    )


def test_extracts_docx_core_properties(tmp_path):
    path = tmp_path / "document.docx"
    document = Document()
    document.core_properties.author = "Document Author"
    document.core_properties.last_modified_by = "Last Editor"
    document.core_properties.created = datetime(2024, 3, 4, 5, 6, 7)
    document.core_properties.modified = datetime(2025, 4, 5, 6, 7, 8)
    document.save(path)

    metadata = extract_document_metadata(str(path))

    assert metadata.author == "Document Author"
    assert metadata.last_modified_by == "Last Editor"
    assert metadata.created_at == "2024-03-04T05:06:07Z"
    assert metadata.modified_at == "2025-04-05T06:07:08Z"


def test_pdf_metadata_is_intentionally_ignored(tmp_path):
    path = tmp_path / "document.pdf"
    path.write_bytes(b"not opened by the metadata reader")

    assert extract_document_metadata(str(path)) == DocumentMetadata()
