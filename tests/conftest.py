import copy
import hashlib
import io
import threading
from pathlib import Path
from uuid import uuid4

import pytest
from PIL import Image

from prompt_studio.auth import Accounts, hash_password
from prompt_studio.characters import CharacterLibrary
from prompt_studio.export import render_sections
from prompt_studio.models import CharacterFields, EditResponse, ReviewResponse, TargetedEdit
from prompt_studio.pdf_source import parse_text
from prompt_studio.pipeline import JobService
from prompt_studio.settings import Settings
from prompt_studio.storage import APP, FOLDER

FIXTURE = Path(__file__).parent / "fixtures" / "two-sections.txt"


class Interruption(BaseException):
    """Simulate process death; application error handlers must not catch it."""


class MemoryDrive:
    def __init__(self):
        self.root_id = "root"
        self.transaction_lock = threading.RLock()
        self.files = {
            "root": {
                "id": "root",
                "name": "Studio",
                "mimeType": FOLDER,
                "parents": [],
                "appProperties": {"studio": APP},
                "data": b"",
            }
        }
        self.hook = None
        self.uploads = []

    def reserve_id(self):
        return uuid4().hex

    def metadata(self, file_id):
        file = self.files.get(file_id)
        return {k: v for k, v in copy.deepcopy(file).items() if k != "data"} if file else None

    def put(self, file_id, name, parent, data, mime):
        if self.hook:
            self.hook("before", name, file_id, data)
        current = self.files.get(file_id)
        if current and current["data"] == data:
            if self.hook:
                self.hook("after", name, file_id, data)
            return
        self.files[file_id] = {
            "id": file_id,
            "name": name,
            "mimeType": mime,
            "parents": [parent] if parent else [],
            "appProperties": {"studio": APP, "sha256": hashlib.sha256(data).hexdigest()},
            "data": bytes(data),
        }
        self.uploads.append((file_id, name))
        if self.hook:
            self.hook("after", name, file_id, data)

    def read(self, file_id):
        return self.files[file_id]["data"]

    def children(self, parent):
        return [
            self.metadata(file["id"]) for file in self.files.values() if parent in file["parents"]
        ]

    def delete(self, file_id):
        for child in self.children(file_id):
            self.delete(child["id"])
        self.files.pop(file_id, None)


class FakeAI:
    def __init__(self):
        self.usage = []
        self.rewrites = 0
        self.reviews = 0
        self.feedback = []
        self.bad_reviews = 0
        self.bad_edits = False

    def rewrite(self, source, character, image, feedback=None):
        self.rewrites += 1
        self.feedback.append(feedback)
        edits = []
        changes = [
            ("He", "She", "pronouns"),
            ("His", "Her", "pronouns"),
            ("his", "her", "pronouns"),
            ("The man", "The woman", "pronouns"),
            ("the man", "the woman", "pronouns"),
            ("a guy", "a woman", "pronouns"),
            ("deep American voice", character.voice, "voice"),
            ("friendly personality", character.personality, "personality"),
            ("black cap, black T-shirt. Bare hands, no gloves.", character.wardrobe, "wardrobe"),
            (
                "gloves, cap backwards, extra people, flour, cinematic color grade.",
                character.negatives,
                "negatives",
            ),
            ("black T-shirt", "purple jacket", "wardrobe"),
        ]
        for section in source.sections:
            for block in section.blocks:
                if not block.editable:
                    continue
                # Choose disjoint spans, just as a real structured response must.
                used = []
                for old, new, trait in changes:
                    if block.text.count(old) != 1:
                        continue
                    start = block.text.index(old)
                    if any(start < end and start + len(old) > beginning for beginning, end in used):
                        continue
                    edits.append(
                        TargetedEdit(
                            block_id=block.id,
                            original=old,
                            replacement=new,
                            trait=trait,
                            reason="Apply saved character trait.",
                        )
                    )
                    used.append((start, start + len(old)))
        if self.bad_edits:
            header = next(b for b in source.sections[0].blocks if b.kind == "shot_header")
            edits.append(
                TargetedEdit(
                    block_id=header.id,
                    original=header.text,
                    replacement="SHOT 9",
                    trait="continuity",
                    reason="Invalid protected edit.",
                )
            )
        self.usage.append(
            {
                "stage": "rewrite",
                "tokens": {"input_tokens": 10, "output_tokens": 20},
                "request_id": f"rewrite-{self.rewrites}",
            }
        )
        return EditResponse(edits=edits)

    def review(self, source, result, character, image):
        self.reviews += 1
        self.usage.append(
            {
                "stage": "review",
                "tokens": {"input_tokens": 30, "output_tokens": 10},
                "request_id": f"review-{self.reviews}",
            }
        )
        if self.reviews <= self.bad_reviews:
            block = next(b for b in source.sections[0].blocks if b.editable)
            return ReviewResponse(
                passed=False,
                issues=[
                    {
                        "block_id": block.id,
                        "category": "protected_meaning",
                        "explanation": "Story changed or obsolete wardrobe remains.",
                    }
                ],
            )
        return ReviewResponse(passed=True, issues=[])


@pytest.fixture
def source():
    return parse_text(FIXTURE.read_text())


@pytest.fixture
def png():
    output = io.BytesIO()
    Image.new("RGB", (30, 40), "purple").save(output, "PNG")
    return output.getvalue()


@pytest.fixture
def fields():
    return CharacterFields(
        display_name="Maya",
        mapping="@image_1 = Maya — an Ethiopian woman with dark curls, a purple jacket and purple gloves. Use exactly as shown.",
        pronouns="she/her",
        voice="warm Ethiopian English voice",
        personality="curious personality",
        wardrobe="purple jacket and purple gloves; the same in every shot",
        negatives="extra people, flour, cinematic color grade.",
        consistency="Keep her face, dark curls, purple jacket and gloves identical in every shot.",
    )


@pytest.fixture
def accounts():
    return Accounts(
        {
            "admin": {
                "password_hash": hash_password("admin-test-password"),
                "role": "admin",
                "enabled": True,
            },
            "creator": {
                "password_hash": hash_password("creator-test-password"),
                "role": "creator",
                "enabled": True,
            },
        }
    )


@pytest.fixture
def admin(accounts):
    return accounts.authenticate("admin", "admin-test-password")


@pytest.fixture
def creator(accounts):
    return accounts.authenticate("creator", "creator-test-password")


@pytest.fixture
def drive():
    return MemoryDrive()


@pytest.fixture
def library(drive, accounts):
    return CharacterLibrary(drive, accounts)


@pytest.fixture
def character(library, admin, fields, png):
    return library.add(admin, fields, png)


@pytest.fixture
def ai():
    return FakeAI()


@pytest.fixture
def jobs(drive, accounts, library, ai):
    return JobService(drive, accounts, library, Settings(), ai_factory=lambda _: ai)


@pytest.fixture
def pdf(source):
    return render_sections(source, "Fixture")
