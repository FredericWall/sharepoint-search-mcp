"""Download an embedding model into the MCP deployment directory."""

from __future__ import annotations

import argparse
from pathlib import Path

DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
DEFAULT_TARGET = Path("mcp_server/models/all-MiniLM-L6-v2")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download and save the sentence-transformers model for deployment"
    )
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--target", type=Path, default=DEFAULT_TARGET)
    args = parser.parse_args()

    from sentence_transformers import SentenceTransformer

    args.target.parent.mkdir(parents=True, exist_ok=True)
    model = SentenceTransformer(args.model)
    model.save_pretrained(str(args.target))
    print(f"Saved {args.model} to {args.target.resolve()}")


if __name__ == "__main__":
    main()
