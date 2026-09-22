"""Exercise the production PostgreSQL backend from a bound CF task.

The output deliberately excludes database credentials and document content.
"""

from __future__ import annotations

import hashlib
import json
import os

from mcp_server.embeddings import Embedder
from mcp_server.postgres_config import resolve_database_url
from mcp_server.postgres_db import PostgresQueryDatabase


def main() -> None:
    database_url = resolve_database_url(required=True)
    schema = os.environ.get("DATABASE_SCHEMA", "sharepoint_mcp")
    database = PostgresQueryDatabase(database_url, schema=schema)
    embedder = Embedder(
        provider=os.environ.get("EMBEDDING_PROVIDER", "local"),
        model=os.environ.get(
            "EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2"
        ),
    )
    results = database.search(embedder.embed("SAP Analytics Cloud"), top_k=3)
    print(
        "POSTGRES_RUNTIME_RESULT="
        + json.dumps(
            {
                "schema": schema,
                "vectors_loaded": database.cached_vector_count,
                "result_count": len(results),
                "scores": [round(result["score"], 6) for result in results],
                "text_fingerprints": [
                    hashlib.sha256(result["text"].encode()).hexdigest()[:12]
                    for result in results
                ],
            },
            sort_keys=True,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
