import pytest

from indexer.chunker import chunk_text


def test_short_text_returns_single_chunk():
    text = "Dies ist ein kurzer Text."
    chunks = chunk_text(text, chunk_size_tokens=500, overlap_tokens=50)
    assert len(chunks) == 1
    assert chunks[0] == text


def test_long_text_splits_into_multiple_chunks():
    # 2000 Wörter erzeugen mehr als einen 500-Token-Chunk
    text = " ".join(["wort"] * 2000)
    chunks = chunk_text(text, chunk_size_tokens=500, overlap_tokens=50)
    assert len(chunks) > 1


def test_chunks_have_overlap():
    text = " ".join([f"w{i}" for i in range(2000)])
    chunks = chunk_text(text, chunk_size_tokens=500, overlap_tokens=50)
    # Das Ende von chunk[0] sollte am Anfang von chunk[1] wieder auftauchen
    tail = chunks[0].split()[-10:]
    assert any(t in chunks[1].split()[:60] for t in tail)


def test_empty_text_returns_empty_list():
    assert chunk_text("", chunk_size_tokens=500, overlap_tokens=50) == []


def test_whitespace_only_returns_empty_list():
    assert chunk_text("   \n  ", chunk_size_tokens=500, overlap_tokens=50) == []


@pytest.mark.parametrize(
    ("chunk_size", "overlap"),
    [(0, 0), (-1, 0), (10, -1), (10, 10), (10, 11)],
)
def test_rejects_invalid_chunk_configuration(chunk_size, overlap):
    with pytest.raises(ValueError):
        chunk_text("content", chunk_size_tokens=chunk_size, overlap_tokens=overlap)
