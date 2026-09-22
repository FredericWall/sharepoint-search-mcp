"""Microsoft Graph API Client: Device Code Auth, File Listing, Download."""

from __future__ import annotations

import os
from dataclasses import dataclass

import msal
import requests

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
SUPPORTED_EXTENSIONS = {".pptx": "pptx", ".docx": "docx", ".pdf": "pdf"}


@dataclass
class GraphFile:
    item_id: str
    name: str
    file_type: str
    size_bytes: int
    modified_at: str
    sharepoint_path: str
    web_url: str = ""


class GraphClient:
    """Greift auf einen SharePoint-Ordner via Microsoft Graph zu."""

    def __init__(self, client_id: str, tenant_id: str, site_hostname: str, site_path: str):
        self._token = self._authenticate(client_id, tenant_id)
        self.site_id = self._resolve_site_id(site_hostname, site_path)
        self.drive_id = self._resolve_drive_id()

    def _authenticate(self, client_id: str, tenant_id: str) -> str:
        """Device Code Flow: gibt Anweisungen auf der Konsole aus."""
        app = msal.PublicClientApplication(
            client_id, authority=f"https://login.microsoftonline.com/{tenant_id}"
        )
        scopes = ["Sites.Read.All", "Files.Read.All"]
        flow = app.initiate_device_flow(scopes=scopes)
        if "user_code" not in flow:
            raise RuntimeError(f"Device flow konnte nicht gestartet werden: {flow}")
        print(flow["message"])  # zeigt URL + Code zum Einloggen
        result = app.acquire_token_by_device_flow(flow)
        if "access_token" not in result:
            raise RuntimeError(f"Auth fehlgeschlagen: {result.get('error_description')}")
        return result["access_token"]

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token}"}

    def _get_json(self, url: str) -> dict:
        resp = requests.get(url, headers=self._headers(), timeout=60)
        resp.raise_for_status()
        return resp.json()

    def _resolve_site_id(self, hostname: str, site_path: str) -> str:
        data = self._get_json(f"{GRAPH_BASE}/sites/{hostname}:{site_path}")
        return data["id"]

    def _resolve_drive_id(self) -> str:
        data = self._get_json(f"{GRAPH_BASE}/sites/{self.site_id}/drive")
        return data["id"]

    def get_folder_item_id(self, folder_path: str) -> str:
        """Löst den Item-ID eines Ordnerpfads relativ zur Document Library auf.

        Der Drive-``root`` IST bereits die Standard-Bibliothek ("Shared
        Documents"/"Documents"). Ein führendes Bibliotheks-Segment im Pfad
        (wie es aus einer SharePoint-Browser-URL kopiert wird) wird daher
        entfernt, sonst würde es doppelt gehängt und Graph liefert 404.
        """
        encoded = folder_path.strip("/")
        for prefix in ("Shared Documents/", "Documents/"):
            if encoded.startswith(prefix):
                encoded = encoded[len(prefix):]
                break
        data = self._get_json(
            f"{GRAPH_BASE}/drives/{self.drive_id}/root:/{encoded}"
        )
        return data["id"]

    def list_files(self, folder_item_id: str) -> list[GraphFile]:
        """Listet rekursiv alle unterstützten Dateien unterhalb eines Ordners."""
        out: list[GraphFile] = []
        stack = [folder_item_id]
        while stack:
            current = stack.pop()
            url = f"{GRAPH_BASE}/drives/{self.drive_id}/items/{current}/children?$top=200"
            while url:
                data = self._get_json(url)
                for item in data.get("value", []):
                    if "folder" in item:
                        stack.append(item["id"])
                        continue
                    ext = os.path.splitext(item["name"])[1].lower()
                    if ext not in SUPPORTED_EXTENSIONS:
                        continue
                    parent = item.get("parentReference", {}).get("path", "")
                    sp_path = f"{parent}/{item['name']}"
                    out.append(
                        GraphFile(
                            item_id=item["id"],
                            name=item["name"],
                            file_type=SUPPORTED_EXTENSIONS[ext],
                            size_bytes=item.get("size", 0),
                            modified_at=item["lastModifiedDateTime"],
                            sharepoint_path=sp_path,
                        )
                    )
                url = data.get("@odata.nextLink")
        return out

    def download_file(self, item_id: str, dest_path: str) -> None:
        """Lädt eine Datei per Stream herunter."""
        url = f"{GRAPH_BASE}/drives/{self.drive_id}/items/{item_id}/content"
        with requests.get(url, headers=self._headers(), stream=True, timeout=300) as r:
            r.raise_for_status()
            with open(dest_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
