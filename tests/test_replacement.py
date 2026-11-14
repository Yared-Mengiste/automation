import pytest

from prompt_studio.models import EditResponse, TargetedEdit
from prompt_studio.replacement import apply_edits, validate_structure


def test_exact_mapping_filename_and_protected_content(source, character, ai, png):
    response = ai.rewrite(source, character, png)
    result = apply_edits(source, response, character, "maya-cake.png")
    report = validate_structure(source, result, character, "maya-cake.png")
    assert report["dialogue_and_timings_preserved"]
    assert [b.text for s in result.sections for b in s.blocks if b.kind == "mapping"] == [
        character.mapping,
        character.mapping,
    ]
    assert [b.text for s in result.sections for b in s.blocks if b.kind == "attachment"] == [
        "ATTACHMENT: Attach maya-cake.png as @image_1."
    ] * 2
    assert [b.text for s in result.sections for b in s.blocks if b.kind == "dialogue"] == [
        b.text for s in source.sections for b in s.blocks if b.kind == "dialogue"
    ]
    assert "purple gloves" in result.text()
    assert "NEGATIVE PROMPT: extra people, flour, cinematic color grade." in result.text()
    assert "A real kitchen with white cabinets. Soft daylight." in result.text()


@pytest.mark.parametrize("kind", ["mapping", "format", "shot_header", "dialogue"])
def test_protected_block_edits_rejected(kind, source, character):
    block = next(b for b in source.sections[0].blocks if b.kind == kind)
    response = EditResponse(
        edits=[
            TargetedEdit(
                block_id=block.id,
                original=block.text,
                replacement="Changed",
                trait="appearance",
                reason="Invalid",
            )
        ]
    )
    with pytest.raises(ValueError, match="protected"):
        apply_edits(source, response, character, "maya-cake.png")


def test_ambiguous_replacement_rejected(source, character):
    block = next(b for b in source.sections[0].blocks if b.text.startswith("WARDROBE"))
    response = EditResponse(
        edits=[
            TargetedEdit(
                block_id=block.id,
                original="black",
                replacement="purple",
                trait="wardrobe",
                reason="Invalid ambiguity",
            )
        ]
    )
    with pytest.raises(ValueError, match="occurring once"):
        apply_edits(source, response, character, "maya-cake.png")


def test_overlapping_edits_rejected(source, character):
    block = next(b for b in source.sections[0].blocks if b.text.startswith("WARDROBE"))
    response = EditResponse(
        edits=[
            TargetedEdit(
                block_id=block.id,
                original=old,
                replacement="purple",
                trait="wardrobe",
                reason="Invalid overlap",
            )
            for old in ["black cap", "cap"]
        ]
    )
    with pytest.raises(ValueError, match="overlap"):
        apply_edits(source, response, character, "maya-cake.png")


def test_story_change_cannot_be_labeled_pronoun(source, character):
    block = next(b for b in source.sections[0].blocks if "He tips" in b.text)
    response = EditResponse(
        edits=[
            TargetedEdit(
                block_id=block.id,
                original="He tips three eggs",
                replacement="She destroys the bowl",
                trait="pronouns",
                reason="Invalid story change",
            )
        ]
    )
    with pytest.raises(ValueError, match="only pronouns"):
        apply_edits(source, response, character, "maya-cake.png")


def test_shared_target_traits_preserved(source, character):
    # A shared black T-shirt is not obsolete merely because the original wore one.
    shared = character.model_copy(update={"wardrobe": "black T-shirt and purple gloves"})
    block = next(b for b in source.sections[0].blocks if b.text.startswith("WARDROBE"))
    result = apply_edits(
        source,
        EditResponse(
            edits=[
                TargetedEdit(
                    block_id=block.id,
                    original="Bare hands, no gloves.",
                    replacement="Purple gloves.",
                    trait="wardrobe",
                    reason="Replace conflicting glove instruction; keep shared T-shirt.",
                )
            ]
        ),
        shared,
        "maya-cake.png",
    )
    assert "WARDROBE: black cap, black T-shirt. Purple gloves." in result.text()
