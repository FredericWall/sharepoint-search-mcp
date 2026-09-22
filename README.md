# SharePoint Search MCP

A self-hosted semantic search service for PPTX, DOCX, and PDF files stored in
SharePoint. A local indexer reads a synchronized OneDrive folder, extracts and chunks
text, creates 384-dimensional MiniLM embeddings, and writes them to SQLite or regular
PostgreSQL. A separate read-only FastMCP server exposes search and retrieval tools over
Streamable HTTP.

The PostgreSQL backend does not require pgvector. Embeddings are stored as float32
BYTEA values and loaded into an in-memory normalized NumPy matrix for cosine search.
This keeps deployment possible on managed PostgreSQL offerings without extension
support, but memory use grows with the number of chunks.

![Architecture](docs/images/sharepoint-search-mcp-architecture.svg)

## Features

- Incremental indexing: unchanged files are skipped and changed files are replaced.
- Per-file transactions and rollback so one corrupt document does not stop a run.
- Exact chunk order and page or slide numbers.
- Clickable SharePoint file links and optional folder links.
- Best-effort embedded Office metadata for PPTX and DOCX.
- SAP IAS or another OIDC discovery endpoint through FastMCP OAuthProxy.
- Privacy-conscious aggregate usage monitoring in PostgreSQL.
- Local SQLite fallback for development and small installations.

## Requirements

- Python 3.11
- A locally synchronized SharePoint or OneDrive folder
- PostgreSQL 16 for the recommended shared deployment, or SQLite for local use
- Cloud Foundry only when using the supplied deployment and tunnel helpers
- An OIDC application for authenticated deployments

## Quick start

Create a virtual environment and install both components:

~~~powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
Copy-Item indexer\.env.example indexer\.env
~~~

Edit indexer/.env so LOCAL_ROOT, LOCAL_PREFIX, SHAREPOINT_PREFIX, and SITE_URL describe
the same synchronized library. Plan runs are read-only:

~~~powershell
python -m indexer.indexer
python -m indexer.indexer --apply
~~~

For a simple local SQLite setup, set these values in indexer/.env:

~~~dotenv
INDEX_BACKEND=sqlite
INDEX_DB_PATH=mcp_server/index.db
~~~

Then start the local MCP server without OIDC:

~~~powershell
$env:MCP_AUTH_DISABLED = "1"
$env:INDEX_BACKEND = "sqlite"
$env:INDEX_DB_PATH = "mcp_server/index.db"
python -m mcp_server.main
~~~

The Streamable HTTP endpoint is http://localhost:8000/mcp.

## PostgreSQL

Start an optional local PostgreSQL 16 instance:

~~~powershell
docker compose up -d
$env:DATABASE_URL = "postgresql://sharepoint_mcp:local-development-only@localhost:5432/sharepoint_mcp"
$env:INDEX_DATABASE_URL = $env:DATABASE_URL
~~~

Set INDEX_BACKEND=postgres and POSTGRES_SCHEMA=sharepoint_mcp for the indexer. The
server uses DATABASE_SCHEMA for the same value. Schema and tables are created by the
application user, which therefore needs the corresponding permissions.

The server preloads all vectors. A 384-dimensional vector occupies 1,536 bytes before
NumPy and process overhead. Size memory from the real chunk count and do not assume the
example 1,800 MB limit fits every corpus.

## Model files

By default sentence-transformers downloads all-MiniLM-L6-v2 on first use. For a
restricted or reproducible deployment, download it before packaging:

~~~powershell
python tools/download_model.py
~~~

The generated mcp_server/models directory is intentionally ignored by Git but is not
excluded from cf push. Set EMBEDDING_MODEL to
mcp_server/models/all-MiniLM-L6-v2 in that case.

## Cloud Foundry deployment

1. Create a PostgreSQL service instance and ensure its application user may create the
   configured schema and tables.
2. Copy vars.example.yml to vars.yml and replace every placeholder. vars.yml is ignored
   by Git because it contains the OIDC secret and monitoring salt.
3. Register the MCP_BASE_URL callback URLs required by FastMCP in your OIDC provider.
4. Download the model if the staging/runtime network cannot reach Hugging Face.
5. Deploy and verify:

~~~powershell
cf push --vars-file vars.yml
cf logs sharepoint-search-mcp --recent
~~~

The service binding is discovered through VCAP_SERVICES. Outside Cloud Foundry, use
DATABASE_URL. The supplied route-less helper in tools/db_tunnel_app and the PowerShell
runner can reach a private CF database from a trusted workstation:

~~~powershell
cf push -f tools/db_tunnel_app/manifest.yml --vars-file tools/db_tunnel_app/vars.yml
.\tools\run_local_onedrive_indexer.ps1
.\tools\run_local_onedrive_indexer.ps1 -Apply
~~~

Optionally set CF_API, CF_ORG, and CF_SPACE before running the scripts. When provided,
the scripts refuse to run against a different target. Never pass -Apply until the plan
and generated SharePoint paths have been reviewed.

## Runtime configuration

| Variable | Purpose |
| --- | --- |
| INDEX_BACKEND | auto, postgres, or sqlite |
| DATABASE_URL | PostgreSQL URL outside a service binding |
| DATABASE_SCHEMA | Server-side schema; default sharepoint_mcp |
| INDEX_DB_PATH | SQLite path when using the local fallback |
| EMBEDDING_MODEL | Hugging Face model name or local model directory |
| SHAREPOINT_LIBRARY_VIEW_URL | Optional AllItems.aspx URL used for folder links |
| MCP_BASE_URL | Public HTTPS server base URL without a trailing slash |
| OIDC_CONFIG_URL | HTTPS OIDC discovery URL |
| OIDC_CLIENT_ID | OIDC client identifier |
| OIDC_CLIENT_SECRET | OIDC secret; never commit it |
| OIDC_AUDIENCE | Optional token audience override |
| MCP_USAGE_HASH_SALT | Long random secret used to pseudonymize subjects |
| MCP_USAGE_RETENTION_DAYS | Optional aggregate usage retention period |
| MCP_USAGE_MONITORING_DISABLED | Set to 1 to disable database monitoring |
| MCP_AUTH_DISABLED | Local development only; disables authentication |

MCP_AUTH_DISABLED is rejected on Cloud Foundry unless MCP_ALLOW_INSECURE_CF=1 is also
set. Do not use that override for an internet-accessible deployment.

## Authentication limitation

With FastMCP 2.12.5, the PostgreSQL key/value adapter persists dynamic client
registrations. Authorization transactions, authorization codes, and some refresh-token
bookkeeping still live in process memory. Existing clients must therefore be tested
across restarts; complete restart-transparent OAuth state is not guaranteed by this
version.

## Usage monitoring and privacy

Monitoring stores daily aggregate tool and HTTP counts plus hashed OIDC subject IDs.
It does not store prompts, query text, file paths, e-mail addresses, IP addresses, or
tokens. Reports are deliberately not MCP tools. CF operators can retrieve them through:

~~~powershell
.\tools\get_usage_metrics.ps1 -Days 7
~~~

Use a secret, installation-specific MCP_USAGE_HASH_SALT and define an appropriate
retention period for your organization.

## Testing

The default suite is offline and never connects to PostgreSQL or downloads a model:

~~~powershell
python -m ruff check .
python -m compileall -q indexer mcp_server tools tests
python -m pytest
~~~

The included suite uses test doubles and temporary SQLite databases; it does not connect
to PostgreSQL. Run database integration checks only against an explicitly disposable
database, never production. Model quality is outside the unit-test boundary; run a
manual smoke test after downloading the configured model.

## Backup and recovery

Back up PostgreSQL according to your provider's policy. The search index is derived
data and can be rebuilt from the synchronized source folder. Keep source mapping and
embedding configuration with operational documentation, but never store secrets there.

## License

Licensed under the Apache License, Version 2.0. See LICENSE.

SharePoint, Microsoft, SAP, SAP BTP, Joule, and other product names are trademarks of
their respective owners. Their mention describes compatible systems and does not imply
endorsement.
