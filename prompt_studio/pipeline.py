"""Single-instance recoverable jobs with immutable snapshots and reserved upload IDs."""

import base64
import hashlib
import io
import logging
import threading
import zipfile
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

from .agents import ClaudeRewriter
from .auth import Accounts
from .characters import CharacterLibrary
from .errors import GenerationError
from .export import render_sections, validate_glyphs
from .models import (
    Character,
    DocumentResponse,
    EditResponse,
    ParsedSource,
    PDFSource,
    ReviewResponse,
    slugify,
    utc_now,
)
from .pdf_source import read_pdf
from .replacement import apply_edits, preview, validate_document, validate_structure
from .settings import Settings
from .storage import FOLDER, Storage, ensure_folder, find_named, json_bytes, read_json

LOG = logging.getLogger("prompt_studio.private")
JOB_GATE = threading.Lock()


class JobService:
    def __init__(
        self,
        storage: Storage,
        accounts: Accounts,
        library: CharacterLibrary,
        settings: Settings,
        ai_factory: Callable = ClaudeRewriter,
        renderer: Callable = render_sections,
        gate=None,
    ):
        self.storage, self.accounts, self.library, self.settings = (
            storage,
            accounts,
            library,
            settings,
        )
        self.ai_factory, self.renderer = ai_factory, renderer
        self.gate = gate if gate is not None else JOB_GATE
        # Cache stable document IDs only; every access reads current durable state.
        self._locations = {}
        with storage.transaction_lock:
            self.folder_id = ensure_folder(storage, storage.root_id, "Jobs")

    @property
    def busy(self):
        return self.gate.locked()

    def check(self, principal, pdf: bytes, name: str, selections: list[Character]):
        self.accounts.require(principal)
        source = read_pdf(pdf, self.settings)
        prompt_slug = slugify(name.rsplit(".", 1)[0])
        if not selections or len({c.id for c in selections}) != len(selections):
            raise ValueError("Select at least one character, without duplicates.")
        with self.storage.transaction_lock:
            for selected in selections:
                current = self.library.get(principal, selected.id)
                if current.status != "active":
                    raise ValueError("Choose active characters only.")
                if (
                    current.revision != selected.revision
                    or current.model_dump() != selected.model_dump()
                ):
                    raise GenerationError(
                        "A selected character changed. Refresh the free preview before generating."
                    )
                self.library.image(principal, current)
                validate_glyphs("\n".join(str(v) for v in current.model_dump().values()))
        return source, prompt_slug, preview(source, selections, prompt_slug)

    def create(self, principal, pdf: bytes, name: str, selections: list[Character]) -> dict:
        self.accounts.require(principal)
        with self.storage.transaction_lock:
            source, prompt_slug, _ = self.check(principal, pdf, name, selections)
            images = {c.id: self.library.image(principal, c) for c in selections}
            job_id = str(uuid4())
            local_date = datetime.now(ZoneInfo("Africa/Addis_Ababa")).date().isoformat()
            folder_name = f"{local_date}_{prompt_slug}_{job_id[:8]}"
            folder_id = ensure_folder(self.storage, self.folder_id, folder_name)
            manifest = {
                "schema_version": 1,
                "id": job_id,
                "folder_id": folder_id,
                "folder_name": folder_name,
                "manifest_id": self.storage.reserve_id(),
                "creator": principal.username,
                "created_at": utc_now(),
                "updated_at": utc_now(),
                "status": "preparing",
                "prompt_slug": prompt_slug,
                "model_settings": {
                    "claude_model": self.settings.claude_model,
                    "api_timeout": self.settings.api_timeout,
                },
                "source": source.model_dump(),
                "source_details": {
                    "original_filename": name,
                    "bytes": len(pdf),
                    "sha256": source.source_sha256,
                    "pages": source.pages,
                },
                "original": {
                    "id": self.storage.reserve_id(),
                    "name": f"{prompt_slug}-original.pdf",
                },
                "snapshots_folder_id": self.storage.reserve_id(),
                "checkpoints_folder_id": self.storage.reserve_id(),
                "parsed_source_id": self.storage.reserve_id(),
                "characters": [
                    {
                        "snapshot": c.model_dump(),
                        "snapshot_json_id": self.storage.reserve_id(),
                        "reference_id": self.storage.reserve_id(),
                        "status": "pending",
                        "stage": "snapshots",
                        "attempts": [],
                        "validation": {},
                        "usage": [],
                        "pdf": {
                            "id": self.storage.reserve_id(),
                            "name": f"{c.slug}-{prompt_slug}.pdf",
                        },
                        "png": {
                            "id": self.storage.reserve_id(),
                            "name": f"{c.slug}-{prompt_slug}.png",
                        },
                    }
                    for c in selections
                ],
                # A brief bootstrap protects even interrupted initial snapshot uploads.
                # It contains only job inputs, never credentials, and is removed after all snapshots persist.
                "bootstrap": {
                    "original_pdf": base64.b64encode(pdf).decode(),
                    "images": {
                        key: base64.b64encode(value).decode() for key, value in images.items()
                    },
                },
            }
            self._persist(manifest)
        self._prepare(manifest)
        return manifest

    def _persist(self, manifest):
        manifest["updated_at"] = utc_now()
        self.storage.put(
            manifest["manifest_id"],
            "job-manifest.json",
            manifest["folder_id"],
            json_bytes(manifest),
            "application/json",
        )
        self._locations[manifest["id"]] = manifest["manifest_id"]

    def _prepare(self, manifest):
        if "bootstrap" not in manifest:
            return
        parent = manifest["folder_id"]
        self.storage.put(manifest["snapshots_folder_id"], "Snapshots", parent, b"", FOLDER)
        self.storage.put(manifest["checkpoints_folder_id"], "Checkpoints", parent, b"", FOLDER)
        snapshots = manifest["snapshots_folder_id"]
        original = base64.b64decode(manifest["bootstrap"]["original_pdf"])
        self.storage.put(
            manifest["original"]["id"],
            manifest["original"]["name"],
            parent,
            original,
            "application/pdf",
        )
        self.storage.put(
            manifest["parsed_source_id"],
            "parsed-source.json",
            snapshots,
            json_bytes(manifest["source"]),
            "application/json",
        )
        for entry in manifest["characters"]:
            char = Character.model_validate(entry["snapshot"])
            self.storage.put(
                entry["snapshot_json_id"],
                f"{char.slug}.json",
                snapshots,
                json_bytes(entry["snapshot"]),
                "application/json",
            )
            image = base64.b64decode(manifest["bootstrap"]["images"][char.id])
            self.storage.put(
                entry["reference_id"], f"{char.slug}.png", snapshots, image, "image/png"
            )
            entry["stage"] = "pending"
        del manifest["bootstrap"]
        manifest["status"] = "pending"
        self._persist(manifest)

    def history(self, principal) -> list[dict]:
        self.accounts.require(principal)
        manifests = []
        for folder in self.storage.children(self.folder_id):
            if folder["mimeType"] != FOLDER:
                continue
            doc = find_named(self.storage, folder["id"], "job-manifest.json")
            if doc:
                manifest = read_json(self.storage, doc["id"])
                self._locations[manifest["id"]] = doc["id"]
                manifests.append(manifest)
        return sorted(manifests, key=lambda m: m["created_at"], reverse=True)

    def get(self, principal, job_id):
        self.accounts.require(principal)
        if job_id in self._locations:
            manifest = read_json(self.storage, self._locations[job_id])
            if manifest["id"] != job_id:
                raise GenerationError("The job document does not match its saved identity.")
            return manifest
        for manifest in self.history(principal):
            if manifest["id"] == job_id:
                return manifest
        raise GenerationError("The shared job could not be found.")

    def _attempt(self, manifest, entry, retry):
        previous = entry["attempts"][-1] if entry["attempts"] else None
        if previous and previous["outcome"] != "exhausted":
            return previous
        if previous and not retry:
            return None
        number = len(entry["attempts"]) + 1
        attempt = {"number": number, "created_at": utc_now(), "outcome": "active", "rounds": []}
        for round_number in range(2):
            prefix = f"{entry['snapshot']['slug']}-attempt-{number}-round-{round_number + 1}"
            attempt["rounds"].append(
                {
                    stage: {"id": self.storage.reserve_id(), "name": f"{prefix}-{stage}.json"}
                    for stage in ["rewrite", "candidate", "review", "validated"]
                }
            )
        entry["attempts"].append(attempt)
        entry["status"], entry["stage"] = "running", "rewrite"
        entry.pop("error", None)
        self._persist(manifest)  # All IDs are durable before any paid call/upload.
        return attempt

    def _load_checkpoint(self, manifest, reference):
        metadata = self.storage.metadata(reference["id"])
        if metadata is None:
            return None
        if metadata["name"] != reference["name"] or manifest[
            "checkpoints_folder_id"
        ] not in metadata.get("parents", []):
            raise GenerationError("A checkpoint does not match its reserved Drive identity.")
        return read_json(self.storage, reference["id"])

    def _write_checkpoint(self, manifest, reference, data):
        self.storage.put(
            reference["id"],
            reference["name"],
            manifest["checkpoints_folder_id"],
            json_bytes(data),
            "application/json",
        )
        return data

    def _stage(self, manifest, entry, stage, progress):
        entry["status"], entry["stage"] = "running", stage
        self._persist(manifest)
        LOG.info(
            "job_id=%s character_id=%s stage=%s", manifest["id"], entry["snapshot"]["id"], stage
        )
        progress(f"{entry['snapshot']['display_name']}: {stage.replace('_', ' ')}")

    def _reconcile_usage(self, manifest, entry, attempt):
        usage = []
        for recorded_attempt in entry["attempts"]:
            for round_number, round_files in enumerate(recorded_attempt["rounds"], 1):
                for stage in ["rewrite", "review"]:
                    checkpoint = self._load_checkpoint(manifest, round_files[stage])
                    if checkpoint:
                        usage.extend(
                            {**item, "attempt": recorded_attempt["number"], "round": round_number}
                            for item in checkpoint.get("usage", [])
                        )
        entry["usage"] = usage

    def _process(self, manifest, entry, retry, progress):
        attempt = self._attempt(manifest, entry, retry)
        if attempt is None:
            return
        char = Character.model_validate(entry["snapshot"])
        document_mode = manifest["source"].get("mode") == "document"
        source_type = PDFSource if document_mode else ParsedSource
        result_type = DocumentResponse if document_mode else ParsedSource
        source = source_type.model_validate(manifest["source"])
        document_args = {}
        if document_mode:
            original_pdf = self.storage.read(manifest["original"]["id"])
            if hashlib.sha256(original_pdf).hexdigest() != source.source_sha256:
                raise GenerationError(
                    "The original PDF snapshot is damaged. Restore it before resuming."
                )
            document_args = {"pdf": original_pdf, "png_filename": entry["png"]["name"]}
        image = self.storage.read(entry["reference_id"])
        if hashlib.sha256(image).hexdigest() != char.image_sha256:
            raise GenerationError(
                "A job reference snapshot is damaged. Restore its original snapshot before resuming."
            )
        ai = None

        def agent():
            nonlocal ai
            if ai is None:
                config = replace(self.settings, **manifest["model_settings"])
                ai = self.ai_factory(config)
            return ai

        feedback, validated = None, None
        for round_number, round_files in enumerate(attempt["rounds"], 1):
            validated = self._load_checkpoint(manifest, round_files["validated"])
            if validated:
                break
            rewrite = self._load_checkpoint(manifest, round_files["rewrite"])
            if rewrite is None:
                self._stage(
                    manifest,
                    entry,
                    "corrective_rewrite" if round_number == 2 else "rewrite",
                    progress,
                )
                provider = agent()
                before = len(provider.usage)
                response = provider.rewrite(source, char, image, feedback=feedback, **document_args)
                rewrite = self._write_checkpoint(
                    manifest,
                    round_files["rewrite"],
                    {"response": response.model_dump(), "usage": provider.usage[before:]},
                )
            candidate = self._load_checkpoint(manifest, round_files["candidate"])
            if candidate is None:
                try:
                    if document_mode:
                        result = DocumentResponse.model_validate(rewrite["response"])
                        structural = validate_document(result)
                    else:
                        response = EditResponse.model_validate(rewrite["response"])
                        result = apply_edits(source, response, char, entry["png"]["name"])
                        structural = validate_structure(source, result, char, entry["png"]["name"])
                    validate_glyphs(result.text())
                    candidate = {"result": result.model_dump(), "structural_validation": structural}
                except ValueError as exc:
                    candidate = {"structural_validation": {"passed": False, "error": str(exc)}}
                self._write_checkpoint(manifest, round_files["candidate"], candidate)
            entry["validation"] = {
                "structural": candidate["structural_validation"],
                "ai_review": {"status": "not_run"},
            }
            self._reconcile_usage(manifest, entry, attempt)
            self._persist(manifest)
            if not candidate["structural_validation"]["passed"]:
                feedback = {"structural_validation": candidate["structural_validation"]}
                continue
            result = result_type.model_validate(candidate["result"])
            review = self._load_checkpoint(manifest, round_files["review"])
            if review is None:
                self._stage(manifest, entry, "ai_review", progress)
                provider = agent()
                before = len(provider.usage)
                response = provider.review(source, result, char, image, **document_args)
                review = self._write_checkpoint(
                    manifest,
                    round_files["review"],
                    {"response": response.model_dump(), "usage": provider.usage[before:]},
                )
            report = ReviewResponse.model_validate(review["response"])
            known_ids = (
                {b.id for s in source.sections for b in s.blocks} if not document_mode else set()
            )
            if not document_mode and any(
                issue.block_id not in known_ids for issue in report.issues
            ):
                feedback = {
                    "review_validation": "AI review referenced an unknown source block; use original source block IDs."
                }
                entry["validation"]["ai_review"] = {"passed": False, **feedback}
                self._persist(manifest)
                continue
            entry["validation"]["ai_review"] = report.model_dump()
            self._reconcile_usage(manifest, entry, attempt)
            self._persist(manifest)
            if report.passed:
                validated = self._write_checkpoint(
                    manifest,
                    round_files["validated"],
                    {
                        "result": result.model_dump(),
                        "validation": entry["validation"],
                        "usage": entry["usage"],
                    },
                )
                break
            feedback = report.model_dump()
        if not validated:
            attempt["outcome"] = "exhausted"
            entry["status"], entry["stage"] = "failed", "validation"
            entry["error"] = (
                "Validation did not pass after one corrective rewrite. Review the reports and retry this character if appropriate."
            )
            self._persist(manifest)
            return
        result = result_type.model_validate(validated["result"])
        if document_mode:
            validate_document(result)
        else:
            validate_structure(source, result, char, entry["png"]["name"])
        entry["validation"], entry["usage"] = validated["validation"], validated["usage"]
        self._stage(manifest, entry, "pdf", progress)
        pdf_meta = self.storage.metadata(entry["pdf"]["id"])
        if pdf_meta is None:
            pdf = self.renderer(result, f"{char.display_name} — {manifest['prompt_slug']}")
            entry["pdf"]["sha256"] = hashlib.sha256(pdf).hexdigest()
            self._persist(manifest)  # Digest and ID precede the upload.
            self.storage.put(
                entry["pdf"]["id"],
                entry["pdf"]["name"],
                manifest["folder_id"],
                pdf,
                "application/pdf",
            )
        self._stage(manifest, entry, "png", progress)
        entry["png"]["sha256"] = hashlib.sha256(image).hexdigest()
        self._persist(manifest)
        self.storage.put(
            entry["png"]["id"], entry["png"]["name"], manifest["folder_id"], image, "image/png"
        )
        # Confirm BOTH durable files before reporting completion, including after uncertain uploads.
        for key in ["pdf", "png"]:
            artifact = entry[key]
            metadata = self.storage.metadata(artifact["id"])
            if (
                not metadata
                or metadata["name"] != artifact["name"]
                or manifest["folder_id"] not in metadata.get("parents", [])
            ):
                raise GenerationError("An output does not match its reserved Drive identity.")
            if hashlib.sha256(self.storage.read(artifact["id"])).hexdigest() != artifact["sha256"]:
                raise GenerationError(
                    "An output checksum does not match its checkpoint. Restore the output before resuming."
                )
        attempt["outcome"] = "complete"
        entry["status"], entry["stage"], entry["completed_at"] = "complete", "complete", utc_now()
        entry.pop("error", None)
        self._persist(manifest)

    def resume(self, principal, job_id, *, character_id=None, retry=False, progress=lambda _: None):
        self.accounts.require(principal)
        if not self.gate.acquire(blocking=False):
            raise GenerationError(
                "Another job is processing. This job is saved; start it from history when capacity is available."
            )
        try:
            manifest = self.get(principal, job_id)
            if character_id and not any(
                c["snapshot"]["id"] == character_id for c in manifest["characters"]
            ):
                raise ValueError("This character is not part of the job.")
            self._prepare(manifest)
            manifest["status"] = "running"
            self._persist(manifest)
            for entry in manifest["characters"]:
                if entry["status"] == "complete" or (
                    character_id and entry["snapshot"]["id"] != character_id
                ):
                    continue
                try:
                    self.accounts.require(principal)
                    self._process(manifest, entry, retry, progress)
                except Exception as exc:  # noqa: BLE001 — checkpoint before moving to next character.
                    LOG.warning(
                        "job_id=%s character_id=%s stage=%s category=%s request_id=%s",
                        job_id,
                        entry["snapshot"]["id"],
                        entry["stage"],
                        type(exc).__name__,
                        getattr(exc, "request_id", None),
                    )
                    entry["status"] = "failed"
                    entry["error"] = (
                        str(exc)
                        if isinstance(exc, GenerationError)
                        else "This character could not finish. Check the validation reports and configuration, then resume or retry."
                    )
                    self._persist(manifest)
            statuses = [c["status"] for c in manifest["characters"]]
            manifest["status"] = "complete" if all(s == "complete" for s in statuses) else "partial"
            self._persist(manifest)
            return manifest
        finally:
            self.gate.release()

    def download(self, principal, job_id, character_id, kind) -> tuple[str, bytes]:
        self.accounts.require(principal)
        if kind not in {"pdf", "png"}:
            raise ValueError("Choose PDF or PNG.")
        manifest = self.get(principal, job_id)
        entry = next(
            (c for c in manifest["characters"] if c["snapshot"]["id"] == character_id), None
        )
        if not entry or entry["status"] != "complete":
            raise GenerationError("This character has no complete pair of outputs yet.")
        return entry[kind]["name"], self.storage.read(entry[kind]["id"])

    def bundle(self, principal, job_id) -> bytes:
        self.accounts.require(principal)
        manifest = self.get(principal, job_id)
        if manifest["status"] != "complete":
            raise GenerationError(
                "Complete every character before downloading the complete-job ZIP."
            )
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                manifest["original"]["name"], self.storage.read(manifest["original"]["id"])
            )
            reports = {}
            for entry in manifest["characters"]:
                for kind in ["pdf", "png"]:
                    artifact = entry[kind]
                    archive.writestr(artifact["name"], self.storage.read(artifact["id"]))
                reports[entry["snapshot"]["slug"]] = entry["validation"]
            archive.writestr(
                "validation-reports.json",
                json_bytes({"human_review_required": True, "characters": reports}),
            )
        return output.getvalue()
