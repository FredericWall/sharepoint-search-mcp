from unittest.mock import patch

from indexer.graph_client import GraphClient, GraphFile


def _client():
    # Auth beim Konstruktor überspringen wir via bereits gesetztem Token
    c = GraphClient.__new__(GraphClient)
    c._token = "fake-token"
    c.site_id = "site123"
    c.drive_id = "drive123"
    return c


def test_list_files_filters_supported_types():
    c = _client()
    root_children = {
        "value": [
            {"name": "deck.pptx", "id": "1", "size": 100,
             "lastModifiedDateTime": "2026-01-01T00:00:00Z",
             "file": {}, "parentReference": {"path": "/root:/folder"}},
            {"name": "notes.txt", "id": "2", "size": 50,
             "lastModifiedDateTime": "2026-01-01T00:00:00Z",
             "file": {}, "parentReference": {"path": "/root:/folder"}},
            {"name": "subfolder", "id": "3",
             "folder": {"childCount": 0},
             "parentReference": {"path": "/root:/folder"}},
        ]
    }
    empty_children = {"value": []}

    # Root-Ordner liefert die children, der Subfolder (rekursiv) liefert leer.
    def fake_get(url):
        if "items/3/children" in url:
            return empty_children
        return root_children

    with patch.object(c, "_get_json", side_effect=fake_get):
        files = c.list_files("folder-id")
    names = [f.name for f in files]
    assert "deck.pptx" in names
    assert "notes.txt" not in names  # nicht unterstützt


def test_get_folder_item_id_strips_leading_library_segment():
    # root des Default-Drives IST bereits "Shared Documents" -> das Segment
    # darf nicht erneut an den Pfad gehaengt werden, sonst 404.
    c = _client()
    captured = {}

    def fake_get(url):
        captured["url"] = url
        return {"id": "folder-id-123"}

    with patch.object(c, "_get_json", side_effect=fake_get):
        result = c.get_folder_item_id("Shared Documents/Data_Analytics/01_Sub")

    assert result == "folder-id-123"
    assert "root:/Data_Analytics/01_Sub" in captured["url"]
    assert "Shared%20Documents" not in captured["url"]
    assert "Shared Documents" not in captured["url"]


def test_get_folder_item_id_without_library_segment_unchanged():
    # Pfad ohne fuehrendes "Shared Documents/" bleibt unveraendert.
    c = _client()
    captured = {}

    def fake_get(url):
        captured["url"] = url
        return {"id": "fid"}

    with patch.object(c, "_get_json", side_effect=fake_get):
        c.get_folder_item_id("Data_Analytics/01_Sub")

    assert "root:/Data_Analytics/01_Sub" in captured["url"]


def test_graphfile_has_expected_fields():
    f = GraphFile(
        item_id="1", name="deck.pptx", file_type="pptx",
        size_bytes=100, modified_at="2026-01-01T00:00:00Z",
        sharepoint_path="/folder/deck.pptx",
    )
    assert f.file_type == "pptx"
    assert f.sharepoint_path == "/folder/deck.pptx"
