import datetime as dt

import pytest

from indexer.local_source import list_local_files, validate_source_mapping

LOCAL_PREFIX = "/root/base"
SP_PREFIX = "/teams/site/Shared Documents/base"
SITE_URL = "https://contoso.sharepoint.com/teams/site/Shared%20Documents/Forms/AllItems.aspx"


def _make(tmp_path, rel, content=b"x"):
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return p


def _run(tmp_path):
    return list_local_files(
        root_dir=str(tmp_path),
        local_prefix=str(tmp_path),
        sharepoint_prefix=SP_PREFIX,
        site_url=SITE_URL,
    )


def test_lists_only_supported_types(tmp_path):
    _make(tmp_path, "deck.pptx")
    _make(tmp_path, "doc.docx")
    _make(tmp_path, "paper.pdf")
    _make(tmp_path, "video.mp4")
    _make(tmp_path, "notes.txt")
    _make(tmp_path, "sub/nested.pptx")

    files = _run(tmp_path)
    names = sorted(f.name for f in files)
    assert names == ["deck.pptx", "doc.docx", "nested.pptx", "paper.pdf"]
    assert all(f.file_type in ("pptx", "docx", "pdf") for f in files)


def test_derives_sharepoint_path(tmp_path):
    _make(tmp_path, "sub/deep/deck.pptx")
    files = _run(tmp_path)
    f = files[0]
    # Praefix ersetzt, Forward-Slashes, kein Backslash
    assert f.sharepoint_path == f"{SP_PREFIX}/sub/deep/deck.pptx"
    assert "\\" not in f.sharepoint_path


def test_builds_clickable_web_url(tmp_path):
    _make(tmp_path, "My Deck.pptx")
    files = _run(tmp_path)
    f = files[0]
    # Direktlink-Format: /:p:/r/<pfad>?csf=1&web=1
    assert "https://contoso.sharepoint.com/:p:/r/" in f.web_url
    assert f.web_url.endswith("?csf=1&web=1")
    # Leerzeichen im Pfad muessen url-encodet sein
    assert "%20" in f.web_url
    assert " " not in f.web_url


def test_web_url_contains_sharepoint_path(tmp_path):
    # Der Datei-Pfad muss vollstaendig (url-encodet) in der URL enthalten sein.
    _make(tmp_path, "sub/deep/My Deck.pptx")
    f = _run(tmp_path)[0]
    assert "My%20Deck.pptx" in f.web_url
    assert "sub/deep" in f.web_url


def test_modified_at_is_iso_utc(tmp_path):
    _make(tmp_path, "deck.pptx")
    files = _run(tmp_path)
    # muss parsebar sein
    parsed = dt.datetime.fromisoformat(files[0].modified_at)
    assert parsed.tzinfo is not None


def test_item_id_is_local_path(tmp_path):
    p = _make(tmp_path, "deck.pptx")
    files = _run(tmp_path)
    assert files[0].item_id == str(p)


def test_excludes_named_directories(tmp_path):
    # Dateien in einem ausgeschlossenen Ordner (z.B. veraltetes Archiv)
    # duerfen nicht indexiert werden, auch nicht in Unterordnern.
    _make(tmp_path, "live.pptx")
    _make(tmp_path, "99_Archive_Jun2nd/old.pptx")
    _make(tmp_path, "99_Archive_Jun2nd/sub/deeper.pptx")

    files = list_local_files(
        root_dir=str(tmp_path),
        local_prefix=str(tmp_path),
        sharepoint_prefix=SP_PREFIX,
        site_url=SITE_URL,
        exclude_dirs=["99_Archive_Jun2nd"],
    )
    names = sorted(f.name for f in files)
    assert names == ["live.pptx"]


def test_rejects_root_outside_local_prefix(tmp_path):
    root = tmp_path / "source"
    other = tmp_path / "other"
    root.mkdir()
    other.mkdir()

    with pytest.raises(ValueError, match="equal to or below"):
        validate_source_mapping(str(root), str(other), SP_PREFIX, SITE_URL)


@pytest.mark.parametrize(
    ("sharepoint_prefix", "site_url"),
    [
        ("teams/site/documents", SITE_URL),
        (r"\teams\site\documents", SITE_URL),
        (SP_PREFIX, "http://contoso.sharepoint.com/teams/site"),
        (SP_PREFIX, "not-a-url"),
    ],
)
def test_rejects_unsafe_sharepoint_mapping(
    tmp_path, sharepoint_prefix, site_url
):
    with pytest.raises(ValueError):
        validate_source_mapping(
            str(tmp_path), str(tmp_path), sharepoint_prefix, site_url
        )
