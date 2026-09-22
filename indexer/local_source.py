"""Lokaler Datei-Reader als Alternative zum Microsoft Graph Client.

Durchläuft einen lokalen Ordner (z.B. den synchronisierten OneDrive-/
SharePoint-Ordner) rekursiv und liefert dieselbe ``GraphFile``-Struktur wie
``graph_client.list_files`` — ohne Login, ohne Download. Der in der DB
gespeicherte ``sharepoint_path`` und die klickbare ``web_url`` werden aus dem
lokalen Pfad abgeleitet, damit Suchergebnisse weiter auf SharePoint zeigen.
"""

from __future__ import annotations

import datetime as dt
import os
from urllib.parse import quote, urlsplit

from indexer.graph_client import SUPPORTED_EXTENSIONS, GraphFile

# SharePoint-Kürzel für das /:X:/r/-Direktlink-Format nach Dateityp.
_SP_TYPE_CHAR = {".pptx": "p", ".docx": "w", ".pdf": "b"}


def validate_source_mapping(
    root_dir: str, local_prefix: str, sharepoint_prefix: str, site_url: str
) -> tuple[str, str]:
    """Validate the local-to-SharePoint mapping before inventory traversal.

    A mismatched local prefix previously produced plausible-looking parent paths.
    Failing before the inventory is safer, especially when deletion reconciliation is
    enabled. The returned paths are absolute and normalized.
    """
    root = os.path.abspath(os.path.normpath(root_dir))
    prefix = os.path.abspath(os.path.normpath(local_prefix))
    try:
        common = os.path.commonpath((root, prefix))
    except ValueError as exc:
        raise ValueError(
            "LOCAL_ROOT and LOCAL_PREFIX must be on the same filesystem"
        ) from exc
    if os.path.normcase(common) != os.path.normcase(prefix):
        raise ValueError(
            "LOCAL_ROOT must be equal to or below LOCAL_PREFIX; refusing to "
            "generate SharePoint paths containing parent traversal"
        )
    if not sharepoint_prefix.startswith("/") or chr(92) in sharepoint_prefix:
        raise ValueError(
            "SHAREPOINT_PREFIX must be an absolute forward-slash path"
        )
    parsed_site = urlsplit(site_url)
    if parsed_site.scheme != "https" or not parsed_site.netloc:
        raise ValueError("SITE_URL must be an absolute HTTPS URL")
    return root, prefix


def sharepoint_direct_url(host: str, sharepoint_path: str) -> str:
    """Gibt einen direkten SharePoint-Dateilink im /:X:/r/-Format zurück.

    Dieser Link öffnet die Datei direkt (kein Ordner-Umweg), ohne GUID.
    Format: ``https://<host>/:<typ>:/r<pfad>?csf=1&web=1``
    """
    ext = os.path.splitext(sharepoint_path)[1].lower()
    type_char = _SP_TYPE_CHAR.get(ext, "r")  # 'r' = generischer Fallback
    encoded = quote(sharepoint_path)
    return f"{host}/:{type_char}:/r{encoded}?csf=1&web=1"


def list_local_files(
    root_dir: str,
    local_prefix: str,
    sharepoint_prefix: str,
    site_url: str,
    exclude_dirs: list[str] | None = None,
) -> list[GraphFile]:
    """Listet rekursiv alle unterstützten Dateien unterhalb von ``root_dir``.

    Args:
        root_dir: Ordner, der durchlaufen wird.
        local_prefix: Lokaler Pfad-Präfix, der durch ``sharepoint_prefix``
            ersetzt wird, um den SharePoint-Pfad abzuleiten.
        sharepoint_prefix: SharePoint-Pfad-Präfix (z.B. ``/teams/.../base``).
        site_url: Basis-URL der SharePoint-Seite (nur Host + Site-Pfad,
            z.B. ``https://contoso.sharepoint.com/sites/knowledge-base``).
            Wird für den direkten ``/:X:/r/``-Dateilink verwendet.
        exclude_dirs: Ordnernamen, die (samt Unterordnern) übersprungen werden,
            z.B. veraltete Archiv-Ordner.

    Returns:
        Liste von ``GraphFile``. ``item_id`` enthält den absoluten lokalen Pfad.
    """
    local_prefix = local_prefix.rstrip("/\\")
    root_dir, local_prefix = validate_source_mapping(
        root_dir, local_prefix, sharepoint_prefix, site_url
    )
    sharepoint_prefix = sharepoint_prefix.rstrip("/")
    # Host für direkte Dateilinks: alles bis inkl. des Site-Segments extrahieren.
    # site_url kann entweder die alte AllItems.aspx-Form oder die neue kurze Form sein.
    host = site_url.split("/teams/")[0] if "/teams/" in site_url else site_url.rstrip("/")
    excluded = set(exclude_dirs or ())

    out: list[GraphFile] = []
    def raise_walk_error(error: OSError) -> None:
        raise RuntimeError(f"Could not inventory local source: {error}") from error

    for dirpath, dirnames, filenames in os.walk(root_dir, onerror=raise_walk_error):
        # Ausgeschlossene Ordner aus der Traversierung entfernen (in-place,
        # damit os.walk gar nicht erst hineinsteigt).
        dirnames[:] = sorted(d for d in dirnames if d not in excluded)
        for name in sorted(filenames):
            ext = os.path.splitext(name)[1].lower()
            if ext not in SUPPORTED_EXTENSIONS:
                continue
            abs_path = os.path.join(dirpath, name)

            rel = os.path.relpath(abs_path, local_prefix).replace("\\", "/")
            sharepoint_path = f"{sharepoint_prefix}/{rel}"
            web_url = sharepoint_direct_url(host, sharepoint_path)

            try:
                size = os.path.getsize(abs_path)
                mtime = os.path.getmtime(abs_path)
            except OSError as exc:
                raise RuntimeError(
                    f"Could not read file metadata during inventory: {abs_path}"
                ) from exc
            modified_at = dt.datetime.fromtimestamp(
                mtime, tz=dt.UTC
            ).isoformat()

            out.append(
                GraphFile(
                    item_id=abs_path,
                    name=name,
                    file_type=SUPPORTED_EXTENSIONS[ext],
                    size_bytes=size,
                    modified_at=modified_at,
                    sharepoint_path=sharepoint_path,
                    web_url=web_url,
                )
            )
    return out
