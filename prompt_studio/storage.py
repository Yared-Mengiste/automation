"""Private drive.file storage. Reserved IDs make uncertain uploads idempotent."""

import hashlib
import io
import json
import logging
import threading
from typing import Protocol

from .errors import GenerationError
from .settings import Settings

FOLDER = "application/vnd.google-apps.folder"
APP = "character-prompt-studio"
LOG = logging.getLogger("prompt_studio.private")


def json_bytes(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")


class Storage(Protocol):
    root_id: str
    transaction_lock: threading.RLock

    def reserve_id(self) -> str: ...
    def metadata(self, file_id: str) -> dict | None: ...
    def put(self, file_id: str, name: str, parent: str | None, data: bytes, mime: str) -> None: ...
    def read(self, file_id: str) -> bytes: ...
    def children(self, parent: str) -> list[dict]: ...
    def delete(self, file_id: str) -> None: ...


def find_named(storage: Storage, parent: str, name: str) -> dict | None:
    found = [item for item in storage.children(parent) if item["name"] == name]
    if len(found) > 1:
        raise GenerationError(
            "Duplicate managed Drive files found. Resolve the duplicate folder or document before continuing."
        )
    return found[0] if found else None


def ensure_folder(storage: Storage, parent: str, name: str) -> str:
    found = find_named(storage, parent, name)
    if found:
        if found["mimeType"] != FOLDER:
            raise GenerationError("A managed Drive folder name is occupied by a file.")
        return found["id"]
    file_id = storage.reserve_id()
    storage.put(file_id, name, parent, b"", FOLDER)
    return file_id


def read_json(storage: Storage, file_id: str):
    try:
        return json.loads(storage.read(file_id))
    except (ValueError, UnicodeError) as exc:
        raise GenerationError(
            "A managed Drive document is damaged. Restore it before continuing."
        ) from exc


def folder_link(folder_id: str) -> str:
    return "https://drive.google.com/drive/folders/" + folder_id


class DriveStorage:
    def __init__(self, settings: Settings, service=None):
        self.transaction_lock = threading.RLock()
        if service is None:
            if not all(
                (
                    settings.drive_client_id,
                    settings.drive_client_secret,
                    settings.drive_refresh_token,
                )
            ):
                raise GenerationError(
                    "Configure GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, and GOOGLE_REFRESH_TOKEN in deployment Secrets."
                )
            import httplib2
            from google.oauth2.credentials import Credentials
            from google_auth_httplib2 import AuthorizedHttp
            from googleapiclient.discovery import build

            credentials = Credentials(
                token=None,
                refresh_token=settings.drive_refresh_token,
                token_uri="https://oauth2.googleapis.com/token",
                client_id=settings.drive_client_id,
                client_secret=settings.drive_client_secret,
                scopes=["https://www.googleapis.com/auth/drive.file"],
            )
            service = build(
                "drive",
                "v3",
                http=AuthorizedHttp(credentials, http=httplib2.Http(timeout=30)),
                cache_discovery=False,
            )
        self.service = service
        self.root_id = self._root(settings.drive_root_id)

    def _execute(self, request, *, missing_ok=False):
        try:
            with self.transaction_lock:
                return request.execute(num_retries=0)
        except Exception as exc:  # noqa: BLE001 — never display raw Google errors.
            status = getattr(getattr(exc, "resp", None), "status", None)
            if missing_ok and status == 404:
                return None
            headers = getattr(exc, "resp", None)
            get_header = getattr(headers, "get", lambda _: None)
            request_id = get_header("x-goog-request-id") or get_header("x-guploader-uploadid")
            LOG.warning(
                "provider=drive category=%s status=%s request_id=%s",
                type(exc).__name__,
                status,
                request_id,
            )
            if type(exc).__name__ == "RefreshError" or status == 401:
                raise GenerationError(
                    "Google Drive authorization expired or was revoked. Reconnect the dedicated Google account with offline drive.file access and replace GOOGLE_REFRESH_TOKEN in Secrets, then restart and resume the job."
                ) from exc
            if status == 403:
                raise GenerationError(
                    "Google Drive denied access or quota. Check the dedicated account, Drive API, folder access, and available storage, then resume."
                ) from exc
            raise GenerationError(
                "Google Drive could not finish the operation. Check connectivity and account storage, then resume; reserved file IDs prevent duplicate uploads."
            ) from exc

    def _list(self, query):
        items, token = [], None
        while True:
            result = self._execute(
                self.service.files().list(
                    q=query,
                    spaces="drive",
                    pageToken=token,
                    pageSize=100,
                    fields="nextPageToken,files(id,name,mimeType,parents,appProperties,webViewLink)",
                )
            )
            items.extend(result.get("files", []))
            token = result.get("nextPageToken")
            if not token:
                return items

    def _root(self, configured):
        if configured:
            root = self.metadata(configured)
            if (
                not root
                or root.get("mimeType") != FOLDER
                or root.get("appProperties", {}).get("kind") != "root"
            ):
                raise GenerationError(
                    "DRIVE_ROOT_ID must identify the private root created by this application."
                )
            return configured
        roots = self._list(
            f"trashed = false and appProperties has {{ key='studio' and value='{APP}' }} and appProperties has {{ key='kind' and value='root' }}"
        )
        if len(roots) > 1:
            raise GenerationError(
                "Multiple application roots found. Set DRIVE_ROOT_ID to the intended app-created root."
            )
        if roots:
            return roots[0]["id"]
        root_id = self.reserve_id()
        self.put(root_id, "Character Prompt Studio", None, b"", FOLDER)
        return root_id

    def reserve_id(self):
        return self._execute(
            self.service.files().generateIds(count=1, space="drive", type="files")
        )["ids"][0]

    def metadata(self, file_id):
        result = self._execute(
            self.service.files().get(
                fileId=file_id, fields="id,name,mimeType,parents,appProperties,webViewLink"
            ),
            missing_ok=True,
        )
        if result and result.get("appProperties", {}).get("studio") != APP:
            raise GenerationError(
                "Refusing to access a file outside the application-managed Drive files."
            )
        return result

    def put(self, file_id, name, parent, data, mime):
        from googleapiclient.http import MediaIoBaseUpload

        digest = hashlib.sha256(data).hexdigest()
        with self.transaction_lock:
            existing = self.metadata(file_id)
            if existing:
                if (
                    existing["name"] != name
                    or existing["mimeType"] != mime
                    or (parent and parent not in existing.get("parents", []))
                ):
                    raise GenerationError(
                        "Reserved Drive file identity does not match its checkpoint."
                    )
                if mime == FOLDER or existing.get("appProperties", {}).get("sha256") == digest:
                    return
            body = {
                "name": name,
                "mimeType": mime,
                "appProperties": {"studio": APP, "sha256": digest},
            }
            if not parent:
                body["appProperties"]["kind"] = "root"
            media = (
                None
                if mime == FOLDER
                else MediaIoBaseUpload(io.BytesIO(data), mimetype=mime, resumable=False)
            )
            if existing:
                request = self.service.files().update(
                    fileId=file_id, body=body, media_body=media, fields="id"
                )
            else:
                body["id"] = file_id
                if parent:
                    body["parents"] = [parent]
                request = self.service.files().create(body=body, media_body=media, fields="id")
            self._execute(request)

    def read(self, file_id):
        if not self.metadata(file_id):
            raise GenerationError(
                "A durable Drive file is missing. Restore the file before resuming."
            )
        return self._execute(self.service.files().get_media(fileId=file_id))

    def children(self, parent):
        if not self.metadata(parent):
            raise GenerationError("The managed Drive folder is missing.")
        # Escape even configured IDs; the Drive query language is not SQL.
        parent = parent.replace("\\", "\\\\").replace("'", "\\'")
        return self._list(
            f"'{parent}' in parents and trashed = false and appProperties has {{ key='studio' and value='{APP}' }}"
        )

    def delete(self, file_id):
        if self.metadata(file_id):
            self._execute(self.service.files().delete(fileId=file_id))
