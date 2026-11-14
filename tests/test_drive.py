from uuid import uuid4

import pytest

from prompt_studio.errors import GenerationError
from prompt_studio.settings import Settings
from prompt_studio.storage import FOLDER, DriveStorage


class ProviderFailure(Exception):
    def __init__(self, status):
        self.resp = type("Response", (), {"status": status})()
        super().__init__("SECRET_RAW_GOOGLE_ERROR")


class Request:
    def __init__(self, fn):
        self.fn = fn

    def execute(self, num_retries):
        assert num_retries == 0
        return self.fn()


class FakeDriveAPI:
    def __init__(self):
        self.items = {}
        self.creates = 0
        self.fail_after_create = False
        self.page_size = 2

    def files(self):
        return self

    def generateIds(self, **kwargs):
        assert kwargs == {"count": 1, "space": "drive", "type": "files"}
        return Request(lambda: {"ids": [uuid4().hex]})

    def get(self, fileId, fields):
        def read():
            if fileId not in self.items:
                raise ProviderFailure(404)
            return {key: value for key, value in self.items[fileId].items() if key != "data"}

        return Request(read)

    def list(self, q, **kwargs):
        def read():
            if "key='kind'" in q:
                items = [
                    item
                    for item in self.items.values()
                    if item.get("appProperties", {}).get("kind") == "root"
                ]
            else:
                parent = q.split("'")[1]
                items = [item for item in self.items.values() if parent in item.get("parents", [])]
            offset = int(kwargs.get("pageToken") or 0)
            page = items[offset : offset + self.page_size]
            response = {
                "files": [
                    {key: value for key, value in item.items() if key != "data"} for item in page
                ]
            }
            if offset + self.page_size < len(items):
                response["nextPageToken"] = str(offset + self.page_size)
            return response

        return Request(read)

    def create(self, body, media_body, fields):
        def save():
            self.creates += 1
            assert body["id"] not in self.items
            assert "permissions" not in body
            data = media_body.getbytes(0, media_body.size()) if media_body else b""
            self.items[body["id"]] = {**body, "data": data}
            if self.fail_after_create:
                self.fail_after_create = False
                raise ProviderFailure(500)
            return {"id": body["id"]}

        return Request(save)

    def update(self, fileId, body, media_body, fields):
        def save():
            self.items[fileId].update(body)
            self.items[fileId]["data"] = media_body.getbytes(0, media_body.size())
            return {"id": fileId}

        return Request(save)

    def get_media(self, fileId):
        return Request(lambda: self.items[fileId]["data"])

    def delete(self, fileId):
        return Request(lambda: self.items.pop(fileId, None))


@pytest.fixture
def adapter():
    api = FakeDriveAPI()
    return DriveStorage(Settings(), service=api), api


def test_root_is_app_created_private_and_reused(adapter):
    storage, api = adapter
    assert api.items[storage.root_id]["appProperties"]["kind"] == "root"
    again = DriveStorage(Settings(), service=api)
    assert again.root_id == storage.root_id
    assert api.creates == 1


def test_unknown_external_root_rejected(adapter):
    storage, api = adapter
    api.items["outside"] = {
        "id": "outside",
        "name": "Outside",
        "mimeType": FOLDER,
        "appProperties": {},
    }
    with pytest.raises(GenerationError, match="outside"):
        DriveStorage(Settings(drive_root_id="outside"), service=api)


def test_uncertain_upload_reconciles_reserved_id(adapter, caplog):
    storage, api = adapter
    file_id = storage.reserve_id()
    api.fail_after_create = True
    with pytest.raises(GenerationError):
        storage.put(file_id, "data.json", storage.root_id, b'{"test":true}', "application/json")
    creates = api.creates
    storage.put(file_id, "data.json", storage.root_id, b'{"test":true}', "application/json")
    assert api.creates == creates
    assert storage.read(file_id) == b'{"test":true}'
    assert "SECRET_RAW_GOOGLE_ERROR" not in caplog.text


def test_manifest_update_and_pagination(adapter):
    storage, api = adapter
    for i in range(5):
        file_id = storage.reserve_id()
        storage.put(file_id, f"{i}.json", storage.root_id, b"old", "application/json")
        storage.put(file_id, f"{i}.json", storage.root_id, b"new", "application/json")
    assert len(storage.children(storage.root_id)) == 5
    assert all(storage.read(item["id"]) == b"new" for item in storage.children(storage.root_id))


def test_reserved_identity_cannot_be_repurposed(adapter):
    storage, api = adapter
    file_id = storage.reserve_id()
    storage.put(file_id, "safe.json", storage.root_id, b"text", "application/json")
    with pytest.raises(GenerationError, match="identity"):
        storage.put(file_id, "other.json", storage.root_id, b"text", "application/json")


def test_revoked_credentials_have_reconnect_instructions(adapter, caplog):
    storage, api = adapter
    with pytest.raises(GenerationError, match="GOOGLE_REFRESH_TOKEN"):
        storage._execute(Request(lambda: (_ for _ in ()).throw(ProviderFailure(401))))
    assert "SECRET_RAW_GOOGLE_ERROR" not in caplog.text


def test_oauth_requests_only_drive_file_and_bounds_http_timeout(monkeypatch):
    import googleapiclient.discovery

    api = FakeDriveAPI()

    def build(api_name, version, **kwargs):
        assert (api_name, version) == ("drive", "v3")
        http = kwargs["http"]
        assert http.credentials.scopes == ["https://www.googleapis.com/auth/drive.file"]
        assert http.http.timeout == 30
        return api

    monkeypatch.setattr(googleapiclient.discovery, "build", build)
    config = Settings(
        drive_client_id="test-client",
        drive_client_secret="test-secret",
        drive_refresh_token="test-token",
    )
    assert DriveStorage(config).root_id
    assert "test-secret" not in repr(config) and "test-token" not in repr(config)
