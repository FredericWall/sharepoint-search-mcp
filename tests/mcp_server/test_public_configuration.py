import pytest

import mcp_server.main as main


def test_folder_link_is_optional(monkeypatch):
    monkeypatch.delenv("SHAREPOINT_LIBRARY_VIEW_URL", raising=False)

    assert main._folder_url("/sites/example/Documents/folder/file.docx") is None


def test_folder_link_rejects_unsafe_base_url(monkeypatch):
    monkeypatch.setenv(
        "SHAREPOINT_LIBRARY_VIEW_URL",
        "https://user:secret@example.test/Forms/AllItems.aspx",
    )

    with pytest.raises(RuntimeError, match="SHAREPOINT_LIBRARY_VIEW_URL"):
        main._folder_url("/sites/example/Documents/folder/file.docx")


def test_folder_link_uses_configured_library(monkeypatch):
    monkeypatch.setenv(
        "SHAREPOINT_LIBRARY_VIEW_URL",
        "https://contoso.sharepoint.com/sites/example/Forms/AllItems.aspx",
    )

    result = main._folder_url("/sites/example/Documents/folder/file.docx")

    assert result == (
        "https://contoso.sharepoint.com/sites/example/Forms/AllItems.aspx"
        "?id=%2Fsites%2Fexample%2FDocuments%2Ffolder"
    )
