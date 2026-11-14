import io
import json
import zipfile

import pytest
from conftest import Interruption
from pypdf import PdfReader

from prompt_studio.errors import GenerationError
from prompt_studio.pipeline import JobService
from prompt_studio.settings import Settings


def restarted(jobs, drive, accounts, library, ai, renderer=None):
    return JobService(
        drive,
        accounts,
        library,
        Settings(claude_model="a-new-model"),
        ai_factory=lambda config: ai,
        renderer=renderer or jobs.renderer,
    )


def test_two_characters_and_shared_history_bundle(
    jobs, library, admin, creator, character, fields, png, pdf, ai, drive
):
    second = library.add(
        admin,
        fields.model_copy(
            update={
                "display_name": "Sara",
                "mapping": "@image_1 = Sara — an Ethiopian woman with dark curls, a purple jacket and purple gloves. Use exactly as shown.",
            }
        ),
        png,
    )
    manifest = jobs.create(creator, pdf, "banana-cake.pdf", [character, second])
    assert manifest["creator"] == "creator"
    assert jobs.history(admin)[0]["id"] == manifest["id"]
    complete = jobs.resume(admin, manifest["id"])
    assert complete["status"] == "complete"
    assert (ai.rewrites, ai.reviews) == (2, 2)
    archive = zipfile.ZipFile(io.BytesIO(jobs.bundle(creator, manifest["id"])))
    assert set(archive.namelist()) == {
        "banana-cake-original.pdf",
        "maya-banana-cake.pdf",
        "maya-banana-cake.png",
        "sara-banana-cake.pdf",
        "sara-banana-cake.png",
        "validation-reports.json",
    }
    for char in [character, second]:
        text = "\n".join(
            p.extract_text()
            for p in PdfReader(io.BytesIO(archive.read(f"{char.slug}-banana-cake.pdf"))).pages
        )
        assert f"{char.slug}-banana-cake.png" in text
        assert 'Man: "Did you know if you mix three eggs"' in text
    assert all(
        entry["pdf"]["id"] in drive.files and entry["png"]["id"] in drive.files
        for entry in complete["characters"]
    )


def test_same_day_job_folders_are_unique(jobs, creator, character, pdf):
    first = jobs.create(creator, pdf, "Cake.pdf", [character])
    second = jobs.create(creator, pdf, "Cake.pdf", [character])
    assert first["folder_name"] != second["folder_name"]
    assert first["folder_id"] != second["folder_id"]


def test_busy_saves_pending_without_ai(jobs, creator, character, pdf, ai):
    jobs.gate.acquire()
    try:
        job = jobs.create(creator, pdf, "Cake.pdf", [character])
        assert job["status"] == "pending"
        assert not ai.rewrites
        with pytest.raises(GenerationError, match="processing"):
            jobs.resume(creator, job["id"])
    finally:
        jobs.gate.release()
    assert jobs.resume(creator, job["id"])["status"] == "complete"


def test_completed_characters_are_skipped(jobs, creator, character, pdf, ai):
    job = jobs.create(creator, pdf, "Cake.pdf", [character])
    jobs.resume(creator, job["id"])
    jobs.resume(creator, job["id"], retry=True)
    assert (ai.rewrites, ai.reviews) == (1, 1)


def test_library_edits_archive_delete_do_not_damage_snapshot_jobs(
    jobs, library, creator, admin, character, fields, png, pdf
):
    job = jobs.create(creator, pdf, "Cake.pdf", [character])
    updated = library.update(
        admin,
        character.id,
        character.revision,
        fields.model_copy(update={"wardrobe": "an entirely different outfit"}),
    )
    archived = library.set_status(admin, character.id, updated.revision, "archived")
    library.delete(admin, character.id, archived.revision, character.slug)
    result = jobs.resume(creator, job["id"])
    assert result["status"] == "complete"
    assert result["characters"][0]["snapshot"]["version"] == 1
    assert result["characters"][0]["snapshot"]["wardrobe"] == fields.wardrobe
    assert jobs.bundle(creator, job["id"])


def test_stale_preview_and_archived_selection_rejected(
    jobs, library, admin, creator, character, fields, pdf
):
    library.update(
        admin,
        character.id,
        character.revision,
        fields.model_copy(update={"personality": "different personality"}),
    )
    with pytest.raises(GenerationError, match="changed"):
        jobs.check(creator, pdf, "Cake.pdf", [character])
    current = library.get(creator, character.id)
    library.set_status(admin, character.id, current.revision, "archived")
    with pytest.raises(ValueError, match="active"):
        jobs.create(creator, pdf, "Cake.pdf", [current])


def test_ai_review_corrects_once(jobs, creator, character, pdf, ai):
    ai.bad_reviews = 1
    job = jobs.create(creator, pdf, "Cake.pdf", [character])
    result = jobs.resume(creator, job["id"])
    assert result["status"] == "complete"
    assert (ai.rewrites, ai.reviews) == (2, 2)
    assert ai.feedback[1]["issues"][0]["category"] == "protected_meaning"


def test_review_repair_exhaustion_and_per_character_retry(jobs, creator, character, pdf, ai):
    ai.bad_reviews = 2
    job = jobs.create(creator, pdf, "Cake.pdf", [character])
    result = jobs.resume(creator, job["id"])
    entry = result["characters"][0]
    assert result["status"] == "partial" and entry["status"] == "failed"
    assert entry["validation"]["structural"]["passed"]
    assert not entry["validation"]["ai_review"]["passed"]
    jobs.resume(creator, job["id"])
    assert ai.rewrites == 2  # Exhausted work needs an explicit retry.
    result = jobs.resume(creator, job["id"], character_id=character.id, retry=True)
    assert result["status"] == "complete"
    assert len(result["characters"][0]["attempts"]) == 2


def test_structural_repair_exhaustion_uses_no_review_calls(jobs, creator, character, pdf, ai):
    ai.bad_edits = True
    job = jobs.create(creator, pdf, "Cake.pdf", [character])
    result = jobs.resume(creator, job["id"])
    assert result["characters"][0]["status"] == "failed"
    assert (ai.rewrites, ai.reviews) == (2, 0)
    assert result["characters"][0]["validation"]["ai_review"]["status"] == "not_run"


@pytest.mark.parametrize("stage", ["rewrite", "candidate", "review", "validated", "pdf", "png"])
@pytest.mark.parametrize("when", ["before", "after"])
def test_restart_at_generation_checkpoint(
    stage, when, jobs, creator, character, pdf, ai, drive, accounts, library
):
    job = jobs.create(creator, pdf, "Cake.pdf", [character])
    fired = False

    def crash(position, name, file_id, data):
        nonlocal fired
        matches = (
            name.endswith(f"-{stage}.json")
            if stage not in {"pdf", "png"}
            else name == f"maya-cake.{stage}"
        )
        if not fired and position == when and matches:
            fired = True
            raise Interruption()

    drive.hook = crash
    with pytest.raises(Interruption):
        jobs.resume(creator, job["id"])
    assert fired and not jobs.busy
    drive.hook = None
    resumed = restarted(jobs, drive, accounts, library, ai).resume(creator, job["id"])
    assert resumed["status"] == "complete"
    assert ai.rewrites == (2 if stage == "rewrite" and when == "before" else 1)
    assert ai.reviews == (2 if stage == "review" and when == "before" else 1)
    names = [f["name"] for f in drive.files.values()]
    assert names.count("maya-cake.pdf") == 1
    assert names.count("maya-cake.png") == 1
    assert resumed["model_settings"]["claude_model"] == jobs.settings.claude_model


@pytest.mark.parametrize(
    "stage",
    [
        "Snapshots",
        "Checkpoints",
        "cake-original.pdf",
        "parsed-source.json",
        "maya.json",
        "maya.png",
    ],
)
@pytest.mark.parametrize("when", ["before", "after"])
def test_restart_during_initial_snapshots(
    stage, when, jobs, creator, character, pdf, ai, drive, accounts, library
):
    fired = False

    def crash(position, name, file_id, data):
        nonlocal fired
        if not fired and position == when and name == stage:
            fired = True
            raise Interruption()

    drive.hook = crash
    with pytest.raises(Interruption):
        jobs.create(creator, pdf, "Cake.pdf", [character])
    drive.hook = None
    job = jobs.history(creator)[0]
    # Even deleting the library during interrupted snapshot creation is safe.
    library.delete(
        accounts.authenticate("admin", "admin-test-password"),
        character.id,
        character.revision,
        character.slug,
    )
    resumed = restarted(jobs, drive, accounts, library, ai).resume(creator, job["id"])
    assert resumed["status"] == "complete"
    assert "bootstrap" not in resumed
    assert (ai.rewrites, ai.reviews) == (1, 1)


@pytest.mark.parametrize("when", ["before", "after"])
def test_failure_between_upload_completion_and_manifest_update(
    when, jobs, creator, character, pdf, ai, drive, accounts, library
):
    job = jobs.create(creator, pdf, "Cake.pdf", [character])
    fired = False

    def crash(position, name, file_id, data):
        nonlocal fired
        if not fired and position == when and name == "job-manifest.json":
            candidate = json.loads(data)
            if candidate["characters"][0]["status"] == "complete":
                fired = True
                raise Interruption()

    drive.hook = crash
    with pytest.raises(Interruption):
        jobs.resume(creator, job["id"])
    drive.hook = None
    result = restarted(jobs, drive, accounts, library, ai).resume(creator, job["id"])
    assert result["status"] == "complete"
    assert (ai.rewrites, ai.reviews) == (1, 1)
    assert sum(name == "maya-cake.pdf" for _, name in drive.uploads) == 1
    assert sum(name == "maya-cake.png" for _, name in drive.uploads) == 1


def test_renderer_failure_preserves_validated_text(
    jobs, creator, character, pdf, ai, drive, accounts, library
):
    job = jobs.create(creator, pdf, "Cake.pdf", [character])
    original_renderer = jobs.renderer

    def fail(*args):
        raise RuntimeError("failure containing private contents")

    jobs.renderer = fail
    partial = jobs.resume(creator, job["id"])
    assert partial["status"] == "partial"
    assert "private contents" not in partial["characters"][0]["error"]
    result = restarted(jobs, drive, accounts, library, ai, renderer=original_renderer).resume(
        creator, job["id"]
    )
    assert result["status"] == "complete"
    assert (ai.rewrites, ai.reviews) == (1, 1)


def test_ordinary_upload_failure_resumes_without_paid_work(
    jobs, creator, character, pdf, ai, drive
):
    job = jobs.create(creator, pdf, "Cake.pdf", [character])
    fired = False

    def fail(position, name, file_id, data):
        nonlocal fired
        if not fired and position == "after" and name == "maya-cake.png":
            fired = True
            raise RuntimeError("uncertain completion")

    drive.hook = fail
    partial = jobs.resume(creator, job["id"])
    assert partial["status"] == "partial"
    drive.hook = None
    assert jobs.resume(creator, job["id"])["status"] == "complete"
    assert (ai.rewrites, ai.reviews) == (1, 1)


def test_noncomplete_jobs_cannot_claim_complete_download(jobs, creator, character, pdf):
    job = jobs.create(creator, pdf, "Cake.pdf", [character])
    with pytest.raises(GenerationError):
        jobs.bundle(creator, job["id"])
    with pytest.raises(GenerationError):
        jobs.download(creator, job["id"], character.id, "pdf")


def test_actual_story_change_is_rejected_even_with_allowed_trait_label(
    jobs, creator, character, pdf, ai
):
    from prompt_studio.models import ReviewResponse, TargetedEdit

    original_rewrite = ai.rewrite

    def malicious(source, char, image, feedback=None):
        response = original_rewrite(source, char, image, feedback)
        block = next(b for b in source.sections[0].blocks if b.editable and "three eggs" in b.text)
        response.edits.append(
            TargetedEdit(
                block_id=block.id,
                original="three eggs",
                replacement="one cup of flour",
                trait="personality",
                reason="Disallowed change concealed as a saved trait.",
            )
        )
        return response

    def review(source, result, char, image):
        assert "one cup of flour" in result.text()
        block = next(b for b in source.sections[0].blocks if b.editable and "three eggs" in b.text)
        ai.reviews += 1
        return ReviewResponse(
            passed=False,
            issues=[
                {
                    "block_id": block.id,
                    "category": "protected_meaning",
                    "explanation": "The action changed eggs to flour; protected story changed.",
                }
            ],
        )

    ai.rewrite, ai.review = malicious, review
    job = jobs.create(creator, pdf, "Cake.pdf", [character])
    result = jobs.resume(creator, job["id"])
    assert result["status"] == "partial"
    assert result["characters"][0]["validation"]["structural"]["passed"]
    assert (
        result["characters"][0]["validation"]["ai_review"]["issues"][0]["category"]
        == "protected_meaning"
    )
    assert (ai.rewrites, ai.reviews) == (2, 2)
    assert jobs.storage.metadata(result["characters"][0]["pdf"]["id"]) is None


def test_unknown_review_block_gets_bounded_correction(jobs, creator, character, pdf, ai):
    from prompt_studio.models import ReviewResponse

    def invalid_review(*args):
        ai.reviews += 1
        return ReviewResponse(
            passed=False,
            issues=[
                {
                    "block_id": "invented-block",
                    "category": "obsolete_trait",
                    "explanation": "Invalid review location.",
                }
            ],
        )

    ai.review = invalid_review
    job = jobs.create(creator, pdf, "Cake.pdf", [character])
    result = jobs.resume(creator, job["id"])
    assert result["characters"][0]["status"] == "failed"
    assert (ai.rewrites, ai.reviews) == (2, 2)
    assert result["characters"][0]["attempts"][0]["outcome"] == "exhausted"


def test_real_sample_two_character_structural_acceptance_and_restart(
    jobs, library, admin, creator, character, fields, png, ai, drive, accounts
):
    from pathlib import Path

    from prompt_studio.pdf_source import read_pdf

    sample = Path("banana-cake.pdf")
    if not sample.is_file():
        pytest.skip("Personal sample is excluded from Git.")
    second = library.add(
        admin,
        fields.model_copy(
            update={
                "display_name": "Sara",
                "mapping": "@image_1 = Sara — a woman with dark curls and a purple jacket. Use exactly as shown.",
            }
        ),
        png,
    )
    original = read_pdf(sample.read_bytes())
    job = jobs.create(creator, sample.read_bytes(), sample.name, [character, second])
    first_pass = jobs.resume(creator, job["id"], character_id=character.id)
    assert first_pass["characters"][0]["status"] == "complete"
    assert first_pass["characters"][1]["status"] == "pending"
    complete = restarted(jobs, drive, accounts, library, ai).resume(admin, job["id"])
    assert complete["status"] == "complete"
    assert (ai.rewrites, ai.reviews) == (2, 2)
    for entry in complete["characters"]:
        output = read_pdf(drive.read(entry["pdf"]["id"]))
        assert [(s.duration, [(x.start, x.end) for x in s.shots]) for s in output.sections] == [
            (s.duration, [(x.start, x.end) for x in s.shots]) for s in original.sections
        ]
        assert [b.text for s in output.sections for b in s.blocks if b.kind == "dialogue"] == [
            b.text for s in original.sections for b in s.blocks if b.kind == "dialogue"
        ]
        assert [b.text for s in output.sections for b in s.blocks if b.kind == "mapping"] == [
            entry["snapshot"]["mapping"]
        ] * 2
        assert entry["png"]["name"] in output.text()
    assert jobs.bundle(admin, job["id"])
