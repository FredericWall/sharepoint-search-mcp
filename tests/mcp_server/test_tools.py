import asyncio
from unittest.mock import MagicMock

import pytest

import mcp_server.main as main


def test_usage_metrics_are_not_exposed_as_mcp_tool():
    tools = asyncio.run(main.mcp.get_tools())
    assert "get_usage_metrics" not in tools


def test_search_documents_tool_formats_results():
    fake_db = MagicMock()
    fake_db.search.return_value = [
        {"text": "Analytics Architektur", "page_or_slide": 3,
         "filename": "deck.pptx", "sharepoint_path": "/f/deck.pptx",
         "web_url": "https://contoso.sharepoint.com/x?id=/f/deck.pptx", "score": 0.91,
         "document_author": "Ada Lovelace",
         "document_created_at": "2024-01-02T03:04:05",
         "document_last_modified_by": "Grace Hopper",
         "document_modified_at": "2025-02-03T04:05:06"},
    ]
    fake_embedder = MagicMock()
    fake_embedder.embed.return_value = [0.1] * 384

    main._db = fake_db
    main._embedder = fake_embedder

    result = main._search_documents("analytics architektur", top_k=5)

    fake_embedder.embed.assert_called_once_with("analytics architektur")
    assert "deck.pptx" in result
    assert "Slide 3" in result
    assert "Analytics Architektur" in result
    assert "https://contoso.sharepoint.com/x?id=/f/deck.pptx" in result
    # Dateiname als Markdown-Link (unterdrückt nicht-klickbare Joule-Chips)
    assert "[deck.pptx](https://contoso.sharepoint.com/x?id=/f/deck.pptx)" in result
    assert "Author: Ada Lovelace" in result
    assert "Last saved by: Grace Hopper" in result
    assert "not authoritative SharePoint metadata" in result


def test_list_files_tool_formats_output():
    fake_db = MagicMock()
    fake_db.list_files.return_value = [
        {"filename": "a.pptx", "sharepoint_path": "/f/a.pptx", "file_type": "pptx",
         "size_bytes": 1024, "modified_at": "2026-01-01 00:00:00+00:00",
         "web_url": "https://contoso.sharepoint.com/x?id=/f/a.pptx",
         "document_author": "Ada", "document_created_at": None,
         "document_last_modified_by": None, "document_modified_at": None},
    ]
    main._db = fake_db
    result = main._list_files(folder_path=None, file_type=None)
    assert "[a.pptx](https://contoso.sharepoint.com/x?id=/f/a.pptx)" in result
    assert "Author: Ada" in result
    fake_db.list_files.assert_called_once_with(
        folder_path=None, file_type=None, limit=101
    )


def test_list_files_reports_truncation():
    fake_db = MagicMock()
    fake_db.list_files.return_value = [
        {
            "filename": f"{index}.pdf",
            "sharepoint_path": f"/f/{index}.pdf",
            "file_type": "pdf",
            "web_url": None,
        }
        for index in range(3)
    ]
    main._db = fake_db

    result = main._list_files(limit=2)

    assert "2 files shown (limit 2" in result
    assert "2.pdf" not in result


@pytest.mark.parametrize("value", [0, -1, 21, True])
def test_search_limit_rejects_out_of_range_values(value):
    with pytest.raises(ValueError, match="top_k"):
        main._validated_limit(value, default=5, maximum=20, name="top_k")


def test_markdown_link_rejects_non_https_and_escapes_label():
    assert main._markdown_link("unsafe]name", "javascript:alert(1)") == (
        "unsafe" + chr(92) + "]name"
    )
    assert main._markdown_link("safe]name", "https://example.test/a") == (
        "[safe" + chr(92) + "]name](https://example.test/a)"
    )
    assert main._markdown_link("line\nbreak", "https://example.test/a") == (
        "[line break](https://example.test/a)"
    )


@pytest.mark.parametrize("value", ["", "relative/file.pptx", "/" + "a" * 4096])
def test_file_path_validation_rejects_invalid_values(value):
    with pytest.raises(ValueError, match="file_path"):
        main._validated_file_path(value)


def test_get_file_metadata_tool_formats_office_properties():
    fake_db = MagicMock()
    fake_db.get_file_metadata.return_value = {
        "filename": "a.docx", "sharepoint_path": "/f/a.docx",
        "file_type": "docx", "size_bytes": 1024,
        "modified_at": "2026-01-01T00:00:00Z",
        "web_url": "https://contoso.sharepoint.com/a.docx",
        "document_author": "Ada",
        "document_created_at": "2024-01-02T03:04:05",
        "document_last_modified_by": "Grace",
        "document_modified_at": "2025-02-03T04:05:06",
    }
    main._db = fake_db

    result = main._get_file_metadata("/f/a.docx")

    assert "[a.docx](https://contoso.sharepoint.com/a.docx)" in result
    assert "Author: Ada" in result
    assert "Created: 2024-01-02T03:04:05" in result
    assert "Last saved by: Grace" in result
    assert "Modified: 2025-02-03T04:05:06" in result


def test_get_file_metadata_tool_explains_pdf_scope_and_missing_file():
    fake_db = MagicMock()
    fake_db.get_file_metadata.return_value = {
        "filename": "a.pdf", "sharepoint_path": "/f/a.pdf",
        "file_type": "pdf", "size_bytes": 1024, "modified_at": None,
        "web_url": None, "document_author": None,
        "document_created_at": None, "document_last_modified_by": None,
        "document_modified_at": None,
    }
    main._db = fake_db
    assert "not collected for PDF" in main._get_file_metadata("/f/a.pdf")

    fake_db.get_file_metadata.return_value = None
    assert "No metadata found" in main._get_file_metadata("/f/missing.pdf")


def test_get_document_content_tool_concatenates_chunks():
    fake_db = MagicMock()
    fake_db.get_document_content.return_value = [
        {"chunk_index": 0, "page_or_slide": 1, "text": "erster teil"},
        {"chunk_index": 1, "page_or_slide": 2, "text": "zweiter teil"},
    ]
    main._db = fake_db
    result = main._get_document_content("/f/a.pptx")
    assert "erster teil" in result
    assert "zweiter teil" in result


def test_get_document_content_handles_missing_file():
    fake_db = MagicMock()
    fake_db.get_document_content.return_value = []
    main._db = fake_db
    result = main._get_document_content("/f/missing.pptx")
    assert "no content found" in result.lower()


def test_status_tags_detects_wip_archiv_old():
    assert "WIP" in main._status_tags("/teams/x/# WIP [Internal]/2023_Q2/deck.pptx")
    assert "ARCHIV" in main._status_tags("/teams/x/99_Archive/deck.pptx")
    assert "OLD" in main._status_tags("/teams/x/aktiv/deck_old.pptx")
    # Datei-Tag ohne Ordner (z.B. '(old)') wird ebenfalls erkannt
    assert "OLD" in main._status_tags("/teams/x/aktiv/onepager(old).pptx")


def test_status_tags_ignores_archiving_content():
    # 'Archiving' ist inhaltlich, kein Archiv-Ordner -> KEIN ARCHIV-Tag
    tag = main._status_tags("/teams/x/Data_Tiering/02_DT_Archiving_in_BW_dataflows.pptx")
    assert "ARCHIV" not in tag


def test_status_tags_clean_path():
    assert main._status_tags("/teams/x/01_Delivery/aktuell/deck.pptx") == ""


def test_search_documents_marks_wip():
    fake_db = MagicMock()
    fake_db.search.return_value = [
        {"text": "Inhalt", "page_or_slide": 1, "filename": "wip_deck.pptx",
         "sharepoint_path": "/teams/x/#WIP [Internal]/wip_deck.pptx",
         "web_url": "https://contoso.sharepoint.com/x", "score": 0.8},
    ]
    fake_embedder = MagicMock()
    fake_embedder.embed.return_value = [0.1] * 384
    main._db = fake_db
    main._embedder = fake_embedder
    result = main._search_documents("test", top_k=5)
    assert "WIP" in result
    assert "wip_deck.pptx" in result


def test_folder_url_builds_allitems_link():
    url = main._folder_url("/teams/site/Shared Documents/base/sub/deck.pptx")
    # Ordner = Pfad ohne Dateiname
    assert "AllItems.aspx" in url
    assert "id=" in url
    # KEIN parent= (löst bei Ordner-Navigation "keine Vorschau" aus)
    assert "parent=" not in url
    # Ordner-Pfad (url-encodet) steht in id, NICHT der Dateiname
    assert "deck.pptx" not in url
    # id endet auf dem Datei-Ordner (base/sub), nichts dahinter
    assert url.endswith("id=%2Fteams%2Fsite%2FShared%20Documents%2Fbase%2Fsub")


def test_folder_url_handles_empty():
    assert main._folder_url(None) is None
    assert main._folder_url("") is None


def test_folder_url_returns_none_without_folder():
    # Pfad ohne "/" -> kein Ordner -> None (statt kaputtem Link)
    assert main._folder_url("deck.pptx") is None


def test_search_documents_shows_clickable_folder_link():
    fake_db = MagicMock()
    fake_db.search.return_value = [
        {"text": "Analytics Architektur", "page_or_slide": 3,
         "filename": "deck.pptx",
         "sharepoint_path": "/teams/site/Shared Documents/base/sub/deck.pptx",
         "web_url": "https://contoso.sharepoint.com/:p:/r/teams/site/x.pptx",
         "score": 0.91},
    ]
    fake_embedder = MagicMock()
    fake_embedder.embed.return_value = [0.1] * 384
    main._db = fake_db
    main._embedder = fake_embedder

    result = main._search_documents("analytics", top_k=5)

    # Ordner-Zeile als klickbarer Markdown-Link statt reiner Path-Text
    assert "Folder:" in result
    assert "AllItems.aspx" in result
    # Link-Text ist der Ordner-Pfad (ohne Dateiname)
    assert "[/teams/site/Shared Documents/base/sub]" in result
    # alte reine 'Path:'-Zeile ist ersetzt
    assert "\n  Path:" not in result
