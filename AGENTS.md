# Repository guidance

## Architecture

This repository contains two deliberately independent Python components:

- indexer/: local document extraction, chunking, embedding, and database writes.
- mcp_server/: a read-only FastMCP search server suitable for Cloud Foundry.

Keep their database and embedding modules independent. The server is deployed without
the indexing pipeline.

## Invariants

- Supported source files are PPTX, DOCX, and PDF. OCR is out of scope.
- Embeddings use all-MiniLM-L6-v2, dimension 384, and cosine similarity.
- PostgreSQL stores embeddings as 1,536-byte float32 BYTEA values; pgvector is not
  required. The MCP server loads a normalized NumPy matrix into memory.
- Re-indexing skips unchanged files and atomically replaces all chunks for changed
  files. One bad file must not abort later files.
- Preserve chunk order, page or slide numbers, and SharePoint file links.
- Keep Streamable HTTP at /mcp and do not expose usage reports as MCP tools.

## Safety

- Never commit credentials, tokens, service keys, populated environment files,
  document content, generated indexes, usage exports, or downloaded model weights.
- Tests must use isolated temporary SQLite databases. PostgreSQL integration tests may
  run only when an explicitly configured disposable database is supplied.
- Indexer runs are plans by default. Database writes require --apply; deletion
  reconciliation requires its additional explicit flag.
- Do not weaken authentication defaults for a deployed Cloud Foundry application.

## Verification

Run these before committing:

    python -m ruff check .
    python -m compileall -q indexer mcp_server tools tests
    python -m pytest
