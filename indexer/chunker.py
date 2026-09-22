"""Teilt Text in überlappende Token-Chunks mittels tiktoken."""

import tiktoken

_ENCODER = tiktoken.get_encoding("cl100k_base")


def chunk_text(
    text: str,
    chunk_size_tokens: int = 500,
    overlap_tokens: int = 50,
) -> list[str]:
    """Zerlegt Text in überlappende Chunks.

    Args:
        text: Der zu zerlegende Text.
        chunk_size_tokens: Maximale Token-Anzahl pro Chunk.
        overlap_tokens: Anzahl überlappender Token zwischen aufeinanderfolgenden Chunks.

    Returns:
        Liste von Text-Chunks. Leere/whitespace-only Eingabe ergibt [].
    """
    if chunk_size_tokens <= 0:
        raise ValueError("chunk_size_tokens muss größer als 0 sein")
    if overlap_tokens < 0 or overlap_tokens >= chunk_size_tokens:
        raise ValueError(
            "overlap_tokens muss mindestens 0 und kleiner als "
            "chunk_size_tokens sein"
        )
    if not text or not text.strip():
        return []

    tokens = _ENCODER.encode(text)
    if len(tokens) <= chunk_size_tokens:
        return [text]

    chunks: list[str] = []
    step = chunk_size_tokens - overlap_tokens

    for start in range(0, len(tokens), step):
        window = tokens[start : start + chunk_size_tokens]
        chunks.append(_ENCODER.decode(window))
        if start + chunk_size_tokens >= len(tokens):
            break

    return chunks
