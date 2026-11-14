"""Document output checks and legacy exact-span structural validation."""

import re

from .models import Character, DocumentResponse, EditResponse, ParsedSource, PDFSource, SourceBlock

RULES = """Saved character descriptions and reference PNG take priority over conflicting source character traits. Replace only appearance, pronouns, wardrobe, voice/accent, personality, character-dependent negatives, and continuity. Preserve shared source/target traits. Keep actions, story, props, camera directions, locations, lighting, style, sound, and dialogue content. Pronoun changes are allowed within otherwise preserved prose. Keep every section, independent timeline, shot number, duration and order. Mapping insertion and attachment filenames are deterministic. Structural validation and AI review are separate; semantic preservation still requires human review before production."""
DOCUMENT_RULES = """Read the original PDF in its own format and replace the primary character using every saved character property and the reference PNG. Saved traits take priority over conflicting source traits; preserve shared traits. Adapt appearance, pronouns, speaker identity, wardrobe, voice/accent, personality, character-dependent negatives and continuity consistently throughout the document. Preserve the story, actions, props, camera directions, locations, lighting, style, sound, spoken dialogue content, all sections, shot order and original timings. Include the exact saved @image_1 mapping and the supplied paired PNG attachment instruction in every prompt section, adding them if absent. Interpret headings, tables, prose, inline dialogue and scanned pages yourself. Preserve relevant introductory notes and document order. Return the complete adapted document as readable text. Preservation is checked by AI review; human review is still required before production."""


def validate_document(result: DocumentResponse) -> dict:
    if not result.content.strip():
        raise ValueError("The model returned an empty document.")
    return {"passed": True, "nonempty_output": True, "preservation_check": "ai_review"}


PRONOUN_WORDS = set(
    [
        "he",
        "she",
        "him",
        "her",
        "his",
        "hers",
        "himself",
        "herself",
        "they",
        "them",
        "their",
        "theirs",
        "themself",
        "themselves",
        "man",
        "woman",
        "guy",
        "girl",
        "boy",
        "male",
        "female",
        "a",
        "an",
        "the",
        "from",
        "image_1",
    ]
)


def apply_edits(
    source: ParsedSource, response: EditResponse, character: Character, png_filename: str
) -> ParsedSource:
    blocks = {b.id: b for s in source.sections for b in s.blocks}
    by_block = {}
    for edit in response.edits:
        block = blocks.get(edit.block_id)
        if block is None or not block.editable or block.kind != "prose":
            raise ValueError("An edit targets a missing or protected source block.")
        if block.text.count(edit.original) != 1:
            raise ValueError(
                "An edit must identify exact original text occurring once in its source block."
            )
        if (
            "\n" in edit.replacement
            or "\r" in edit.replacement
            or re.search(r"(?:SHOT\s+\d|FORMAT\s*:|Reference Mapping\s*:)", edit.replacement, re.I)
        ):
            raise ValueError("Edits cannot insert structural labels or line breaks.")
        if set(re.findall(r"@image_[\w-]+", edit.replacement)) - {"@image_1"}:
            raise ValueError("An edit introduced an unsupported reference.")
        if edit.trait == "pronouns":
            allowed = PRONOUN_WORDS | set(re.findall(r"\w+", character.display_name.casefold()))
            words = set(re.findall(r"\w+", (edit.original + " " + edit.replacement).casefold()))
            if not words <= allowed:
                raise ValueError(
                    "Pronoun edits must change only pronouns or primary character nouns."
                )
        start = block.text.index(edit.original)
        by_block.setdefault(block.id, []).append(
            (start, start + len(edit.original), edit.replacement)
        )
    result = source.model_copy(deep=True)
    for section in result.sections:
        for block in section.blocks:
            spans = sorted(by_block.get(block.id, []))
            if any(left[1] > right[0] for left, right in zip(spans, spans[1:], strict=False)):
                raise ValueError("Edits overlap. Return unambiguous, disjoint replacements.")
            for start, end, replacement in reversed(spans):
                block.text = block.text[:start] + replacement + block.text[end:]
            if block.kind == "mapping":
                block.text = character.mapping
        index = next(i for i, b in enumerate(section.blocks) if b.kind == "mapping")
        section.blocks.insert(
            index + 1,
            SourceBlock(
                id=f"s{section.number}-attachment",
                kind="attachment",
                text=f"ATTACHMENT: Attach {png_filename} as @image_1.",
            ),
        )
    validate_structure(source, result, character, png_filename)
    return result


def validate_structure(
    source: ParsedSource, result: ParsedSource, character: Character, filename: str
) -> dict:
    if len(source.sections) != len(result.sections):
        raise ValueError("Prompt sections changed.")
    for before, after in zip(source.sections, result.sections, strict=True):
        if (before.number, before.duration, before.declared_shots, before.shots) != (
            after.number,
            after.duration,
            after.declared_shots,
            after.shots,
        ):
            raise ValueError("Shot order or section timing changed.")
        core = [b for b in after.blocks if b.kind != "attachment"]
        if len(before.blocks) != len(core):
            raise ValueError("Source blocks were added or removed.")
        for original, rewritten in zip(before.blocks, core, strict=True):
            if (original.id, original.kind, original.editable) != (
                rewritten.id,
                rewritten.kind,
                rewritten.editable,
            ):
                raise ValueError("Source block identity changed.")
            if original.kind == "mapping":
                if rewritten.text != character.mapping:
                    raise ValueError("The character mapping must remain exact.")
            elif not original.editable and original.text != rewritten.text:
                raise ValueError("Protected shot headers, timing, or dialogue changed.")
        attachments = [b.text for b in after.blocks if b.kind == "attachment"]
        if attachments != [f"ATTACHMENT: Attach {filename} as @image_1."]:
            raise ValueError("The reference attachment filename does not match the paired PNG.")
    if set(re.findall(r"@image_[\w-]+", result.text())) - {"@image_1"}:
        raise ValueError("Unexpected reference in rewritten text.")
    return {
        "passed": True,
        "sections": len(result.sections),
        "shots": sum(len(s.shots) for s in result.sections),
        "exact_mapping": True,
        "paired_filename": filename,
        "dialogue_and_timings_preserved": True,
    }


def preview(source: PDFSource, characters: list[Character], prompt_slug: str) -> dict:
    return {
        "pages": source.pages,
        "input_mode": source.mode,
        "characters": [
            {
                "id": c.id,
                "name": c.display_name,
                "version": c.version,
                "exact_mapping": c.mapping,
                "png_filename": f"{c.slug}-{prompt_slug}.png",
            }
            for c in characters
        ],
        "rules": DOCUMENT_RULES,
    }
