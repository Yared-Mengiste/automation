"""Admin-only library operations; immutable IDs, slugs and job-independent versions."""

import hashlib

from .auth import Accounts, Principal
from .errors import GenerationError
from .export import validate_glyphs
from .media import normalize_png
from .models import Character, CharacterFields, slugify, utc_now
from .storage import FOLDER, Storage, ensure_folder, find_named, json_bytes, read_json


class CharacterLibrary:
    def __init__(self, storage: Storage, accounts: Accounts):
        self.storage, self.accounts = storage, accounts
        with storage.transaction_lock:
            self.folder_id = ensure_folder(storage, storage.root_id, "Characters")

    def list(self, principal: Principal, include_archived=False) -> list[Character]:
        self.accounts.require(principal)
        result = []
        with self.storage.transaction_lock:
            for folder in self.storage.children(self.folder_id):
                if folder["mimeType"] != FOLDER:
                    continue
                doc = find_named(self.storage, folder["id"], "character.json")
                if doc:
                    character = Character.model_validate(read_json(self.storage, doc["id"]))
                    if include_archived or character.status == "active":
                        result.append(character)
        return sorted(result, key=lambda c: c.display_name.casefold())

    def get(self, principal, character_id) -> Character:
        for character in self.list(principal, include_archived=True):
            if character.id == character_id:
                return character
        raise GenerationError("Character no longer exists. Refresh the library.")

    def image(self, principal, character: Character) -> bytes:
        self.accounts.require(principal)
        data = self.storage.read(character.image_id)
        if hashlib.sha256(data).hexdigest() != character.image_sha256:
            raise GenerationError(
                "The character reference changed outside the application. Replace its image before generating."
            )
        return data

    def _validate_fields(self, fields: CharacterFields):
        fields = CharacterFields.model_validate(fields.model_dump())
        validate_glyphs("\n".join(str(value) for value in fields.model_dump().values()))
        return fields

    def _duplicates(self, principal, name, slug, excluded=None):
        for existing in self.list(principal, include_archived=True):
            if existing.id == excluded:
                continue
            if existing.display_name.casefold() == name.casefold():
                raise ValueError(
                    "A character with this name already exists, including archived characters."
                )
            if existing.slug == slug:
                raise ValueError("A character with this filename slug already exists.")

    def add(
        self, principal, fields: CharacterFields, image: bytes, slug: str | None = None
    ) -> Character:
        self.accounts.require(principal, admin=True)
        fields = self._validate_fields(fields)
        png = normalize_png(image)
        slug = slugify(slug or fields.display_name)
        with self.storage.transaction_lock:
            self._duplicates(principal, fields.display_name, slug)
            folder_id = ensure_folder(self.storage, self.folder_id, slug)
            # Reuse an orphaned reserved image after an interrupted initial add.
            found = find_named(self.storage, folder_id, "reference.png")
            image_id = found["id"] if found else self.storage.reserve_id()
            found = find_named(self.storage, folder_id, "character.json")
            document_id = found["id"] if found else self.storage.reserve_id()
            character = Character(
                **fields.model_dump(),
                slug=slug,
                folder_id=folder_id,
                document_id=document_id,
                image_id=image_id,
                image_sha256=hashlib.sha256(png).hexdigest(),
            )
            self.storage.put(image_id, "reference.png", folder_id, png, "image/png")
            self._save(character)
            return character

    def _save(self, character):
        self.storage.put(
            character.document_id,
            "character.json",
            character.folder_id,
            json_bytes(character.model_dump()),
            "application/json",
        )

    def update(
        self,
        principal,
        character_id,
        expected_revision: int,
        fields: CharacterFields,
        image: bytes | None = None,
    ) -> Character:
        self.accounts.require(principal, admin=True)
        fields = self._validate_fields(fields)
        png = normalize_png(image) if image is not None else None
        with self.storage.transaction_lock:
            current = self.get(principal, character_id)
            if current.revision != expected_revision:
                raise GenerationError(
                    "This character changed in another session. Refresh before editing."
                )
            self._duplicates(principal, fields.display_name, current.slug, excluded=current.id)
            changes = fields.model_dump()
            changed = any(getattr(current, key) != value for key, value in changes.items())
            image_hash = (
                hashlib.sha256(png).hexdigest() if png is not None else current.image_sha256
            )
            image_changed = image_hash != current.image_sha256
            if not changed and not image_changed:
                return current
            if image_changed:
                # Upload an immutable new reference before publishing its pointer.
                # An interrupted edit leaves the old document/image consistent.
                image_id = self.storage.reserve_id()
                self.storage.put(image_id, "reference.png", current.folder_id, png, "image/png")
                changes.update(image_id=image_id, image_sha256=image_hash)
            updated = current.model_copy(
                update={
                    **changes,
                    "version": current.version + 1,
                    "revision": current.revision + 1,
                    "updated_at": utc_now(),
                }
            )
            self._save(updated)
            if image_changed:
                self.storage.delete(current.image_id)
            return updated

    def set_status(self, principal, character_id, expected_revision, status):
        self.accounts.require(principal, admin=True)
        if status not in {"active", "archived"}:
            raise ValueError("Unsupported character status.")
        with self.storage.transaction_lock:
            current = self.get(principal, character_id)
            if current.revision != expected_revision:
                raise GenerationError(
                    "This character changed in another session. Refresh before editing."
                )
            if current.status == status:
                return current
            updated = current.model_copy(
                update={"status": status, "revision": current.revision + 1, "updated_at": utc_now()}
            )
            self._save(updated)
            return updated

    def delete(self, principal, character_id, expected_revision, confirmation):
        self.accounts.require(principal, admin=True)
        with self.storage.transaction_lock:
            current = self.get(principal, character_id)
            if current.revision != expected_revision:
                raise GenerationError(
                    "This character changed in another session. Refresh before deleting."
                )
            if confirmation != current.slug:
                raise ValueError("Type the immutable filename slug to confirm permanent deletion.")
            self.storage.delete(current.folder_id)
