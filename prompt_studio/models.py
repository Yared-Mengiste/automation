"""Durable, versioned contracts for character replacement."""

import re
import unicodedata
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def slugify(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", value).strip("-")[:70].rstrip("-")
    if not slug:
        raise ValueError("Provide an ASCII filename slug using letters or numbers.")
    return slug


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class CharacterFields(Contract):
    display_name: str = Field(min_length=1, max_length=120)
    mapping: str = Field(min_length=15, max_length=6000)
    pronouns: str = Field(min_length=1, max_length=300)
    voice: str = Field(min_length=1, max_length=1500)
    personality: str = Field(min_length=1, max_length=1500)
    wardrobe: str = Field(min_length=1, max_length=2000)
    negatives: str = Field(min_length=1, max_length=3000)
    consistency: str = Field(min_length=1, max_length=3000)

    @field_validator("*", mode="before")
    @classmethod
    def nonempty(cls, value):
        if isinstance(value, str) and not value.strip():
            raise ValueError("Character descriptions must not be blank.")
        return value

    @field_validator("display_name")
    @classmethod
    def clean_name(cls, value):
        return value.strip()

    @field_validator("mapping")
    @classmethod
    def exact_mapping(cls, value):
        if "\n" in value or "\r" in value or not re.fullmatch(r"@image_1\s*=\s*\S.*", value):
            raise ValueError("Save one exact mapping line beginning @image_1 = description.")
        if set(re.findall(r"@image_[\w-]+", value)) != {"@image_1"}:
            raise ValueError("Only @image_1 is supported.")
        return value


class Character(CharacterFields):
    id: str = Field(default_factory=lambda: str(uuid4()))
    slug: str
    status: Literal["active", "archived"] = "active"
    version: int = Field(default=1, ge=1)
    revision: int = Field(default=1, ge=1)
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)
    folder_id: str
    document_id: str
    image_id: str
    image_sha256: str


class SourceBlock(Contract):
    id: str
    kind: Literal["mapping", "format", "shot_header", "dialogue", "prose", "attachment"]
    text: str
    editable: bool = False


class SourceShot(Contract):
    number: int
    start: float
    end: float
    header_id: str


class PromptSection(Contract):
    number: int
    duration: float
    declared_shots: int
    blocks: list[SourceBlock]
    shots: list[SourceShot]


class ParsedSource(Contract):
    schema_version: int = 1
    sections: list[PromptSection]
    pages: int = 0
    extracted_characters: int = 0
    source_sha256: str = ""

    def text(self) -> str:
        return "\n\n".join(b.text for s in self.sections for b in s.blocks)


class PDFSource(Contract):
    mode: Literal["document"] = "document"
    pages: int
    source_sha256: str


class DocumentResponse(Contract):
    content: str = Field(min_length=1)

    def text(self) -> str:
        return self.content


Trait = Literal[
    "appearance",
    "pronouns",
    "wardrobe",
    "voice",
    "accent",
    "personality",
    "negatives",
    "continuity",
]


class TargetedEdit(Contract):
    block_id: str
    original: str = Field(min_length=1)
    replacement: str
    trait: Trait
    reason: str = Field(min_length=1, max_length=1000)


class EditResponse(Contract):
    edits: list[TargetedEdit] = Field(max_length=300)


class ReviewIssue(Contract):
    block_id: str
    category: Literal["obsolete_trait", "protected_meaning", "missing_trait"]
    explanation: str = Field(min_length=1, max_length=2000)


class ReviewResponse(Contract):
    passed: bool
    issues: list[ReviewIssue] = Field(max_length=100)

    @model_validator(mode="after")
    def coherent(self):
        if self.passed == bool(self.issues):
            raise ValueError("A passed review must have no issues; a failed review needs issues.")
        return self
