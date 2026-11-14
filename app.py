"""Character Prompt Studio: authenticated, shared PDF jobs backed by Google Drive."""

import io
import os
import threading
import time
import zipfile
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

from prompt_studio.auth import Accounts, sign_out
from prompt_studio.characters import CharacterLibrary
from prompt_studio.errors import GenerationError
from prompt_studio.logging_utils import configure_private_logging
from prompt_studio.models import CharacterFields
from prompt_studio.pipeline import JobService
from prompt_studio.settings import Settings
from prompt_studio.storage import DriveStorage, folder_link
from prompt_studio.ui import (
    apply_styles,
    character_card,
    configuration_indicators,
    empty_state,
    job_counts,
    page_header,
    status_badge,
    step_header,
)

configure_private_logging()
load_dotenv(Path(__file__).resolve().parent / ".env")
st.set_page_config(page_title="Character Prompt Studio", page_icon="📄", layout="wide")


def configuration():
    try:
        values = dict(st.secrets)
    except FileNotFoundError:
        values = {}
    except Exception as exc:  # noqa: BLE001 — Secrets parsing errors can include credential fragments.
        raise GenerationError(
            "Deployment Secrets could not be read. Check the TOML syntax and restart the app."
        ) from exc
    for name in [
        "APP_USERS_JSON",
        "ANTHROPIC_API_KEY",
        "CLAUDE_MODEL",
        "GOOGLE_CLIENT_ID",
        "GOOGLE_CLIENT_SECRET",
        "GOOGLE_REFRESH_TOKEN",
        "DRIVE_ROOT_ID",
    ]:
        if name in os.environ:
            values[name] = os.environ[name]
    return values


@st.cache_resource
def processing_gate():
    return threading.Lock()


@st.cache_resource
def login_attempts():
    return threading.Lock(), []


@st.cache_resource
def services(settings, accounts_config):
    accounts = Accounts(accounts_config)
    storage = DriveStorage(settings)
    library = CharacterLibrary(storage, accounts)
    jobs = JobService(storage, accounts, library, settings, gate=processing_gate())
    return library, jobs


def safe_error(exc):
    if isinstance(exc, GenerationError):
        return str(exc)
    if isinstance(exc, ValueError) and type(exc).__name__ != "ValidationError":
        return str(exc)
    if type(exc).__name__ == "ValidationError":
        return "Provide complete character descriptions and one exact @image_1 mapping line."
    return "This operation could not finish. Check configuration and connectivity, then resume from Job History."


def login(accounts):
    principal = st.session_state.get("principal")
    if principal:
        try:
            accounts.require(principal)
            return principal
        except GenerationError:
            sign_out(st.session_state)
    with st.container(key="studio-login"):
        st.badge("Your character workspace", color="violet")
        st.title("Character Prompt Studio")
        st.write("Bring your characters into every story.")
        st.caption(
            "Sign in to your shared studio to adapt prompt PDFs, manage character references, and collect finished outputs."
        )
        with st.form("sign-in"):
            username = st.text_input("Username", max_chars=100)
            password = st.text_input("Password", type="password", max_chars=1024)
            submitted = st.form_submit_button("Sign in", type="primary", width="stretch")
        st.caption("Shared workspace · Private Google Drive storage")
    if submitted:
        lock, attempts = login_attempts()
        with lock:
            now = time.monotonic()
            attempts[:] = [t for t in attempts if now - t < 60]
            allowed = len(attempts) < 10
            if allowed:
                attempts.append(now)
        principal = accounts.authenticate(username, password) if allowed else None
        if principal:
            st.session_state.principal = principal
            st.rerun()
        st.error("Invalid credentials or too many attempts. Wait a minute before trying again.")
    st.stop()


def character_form(prefix, current=None):
    values = current.model_dump() if current else {}
    st.subheader("Identity and reference image")
    st.caption(
        "All descriptions are required. The saved mapping is inserted exactly into each PDF."
    )
    fields = {
        "display_name": st.text_input(
            "Display name",
            value=values.get("display_name", ""),
            max_chars=120,
            key=f"{prefix}-name",
        ),
        "mapping": st.text_area(
            "Exact @image_1 mapping",
            value=values.get("mapping", ""),
            height=130,
            max_chars=6000,
            key=f"{prefix}-mapping",
            help="One line beginning @image_1 =. This saved text is inserted exactly.",
        ),
    }
    limits = {
        "pronouns": 300,
        "voice": 1500,
        "personality": 1500,
        "wardrobe": 2000,
        "negatives": 3000,
        "consistency": 3000,
    }
    labels = {
        "pronouns": "Pronouns",
        "voice": "Voice and accent",
        "personality": "Personality",
        "wardrobe": "Wardrobe",
        "negatives": "Character negatives",
        "consistency": "Consistency instructions",
    }
    fields["pronouns"] = st.text_area(
        labels["pronouns"],
        value=values.get("pronouns", ""),
        max_chars=limits["pronouns"],
        key=f"{prefix}-pronouns",
    )
    image = st.file_uploader(
        "Replace reference image" if current else "Reference image",
        type=["png", "jpg", "jpeg", "webp"],
        key=f"{prefix}-image",
    )
    for title, names in [
        ("Voice and personality", ["voice", "personality"]),
        ("Wardrobe and consistency", ["wardrobe", "negatives", "consistency"]),
    ]:
        st.subheader(title)
        for name in names:
            fields[name] = st.text_area(
                labels[name],
                value=values.get(name, ""),
                max_chars=limits[name],
                key=f"{prefix}-{name}",
            )
    return fields, image


def generate_page(principal, library, jobs):
    page_header(
        "Generate",
        "A new cast, the same story. Replace the primary character while preserving each section’s timeline and dialogue.",
    )
    active = library.list(principal)
    if not active:
        empty_state(
            "Your cast starts here",
            "Open Characters to add or restore a character. The admin needs to add an active character before you can generate.",
        )
        return
    upload_column, selection_column = st.columns(2, gap="large")
    with upload_column, st.container(border=True, key="studio-upload"):
        step_header(1, "Upload your prompt", "Start with the original, text-based PDF.")
        upload = st.file_uploader("Original prompt PDF", type=["pdf"])
        st.caption(
            "Limits: 10 MB, 30 text-based pages, 40,000 extracted characters, @image_1 only."
        )
    with selection_column, st.container(border=True, key="studio-selection"):
        step_header(
            2, "Choose your cast", "Each selected character gets a separate PDF and reference PNG."
        )
        selected_ids = st.multiselect(
            "Characters",
            options=[c.id for c in active],
            format_func=lambda key: next(
                f"{c.display_name} · v{c.version}" for c in active if c.id == key
            ),
        )
        st.caption(f"{len(active)} active characters · {len(selected_ids)} selected")
    selected = [c for c in active if c.id in selected_ids]
    for start in range(0, len(selected), 3):
        for column, character in zip(st.columns(3), selected[start : start + 3], strict=False):
            with column, st.container(border=True, key=f"studio-character-selected-{character.id}"):
                character_card(principal, library, character, selected=True)
    if upload is None or not selected:
        with st.container(border=True, key="studio-preview"):
            step_header(
                3,
                "Review the preview",
                "Upload a PDF and choose at least one character to unlock a free preview.",
            )
        return
    # Free preview is rebuilt from current inputs and versions; stale previews cannot start paid work.
    source, slug, checked = jobs.check(principal, upload.getvalue(), upload.name, selected)
    with st.container(border=True, key="studio-preview"):
        step_header(
            3, "Review the preview", "Check the structure and output plan before generation."
        )
        st.success("PDF structure passed. This preview uses no AI calls.")
        section_count, shot_count, output_count = st.columns(3)
        section_count.metric("Sections", len(source.sections))
        shot_count.metric("Shots", sum(len(section.shots) for section in source.sections))
        output_count.metric("Character outputs", len(selected))
        st.dataframe(
            [
                {
                    "Section": section["number"],
                    "Duration (seconds)": section["duration_seconds"],
                    "Shots": section["shot_count"],
                }
                for section in checked["sections"]
            ],
            hide_index=True,
            width="stretch",
        )
        with st.expander("Extracted prompts", expanded=False):
            st.code(checked["extracted_prompts"], language=None, wrap_lines=True)
        with st.expander("Output filenames and preservation rules"):
            st.dataframe(checked["characters"], hide_index=True, width="stretch")
            st.write(checked["rules"])
    with st.container(border=True, key="studio-generation"):
        step_header(
            4,
            "Generate your outputs",
            "Save a shared job and create a prompt PDF for each character.",
        )
        notice, action = st.columns([3, 1], vertical_alignment="center")
        with notice:
            st.caption(
                f"Generate makes paid Claude calls using {jobs.settings.claude_model}. The prompt and selected character snapshots are sent to Anthropic and saved in the private Drive folder."
            )
        with action:
            generate = st.button("Save and generate", type="primary", width="stretch")
        if jobs.busy:
            st.info("Another job is processing. Your new job will be saved as pending.")
    if generate:
        manifest = jobs.create(principal, upload.getvalue(), upload.name, selected)
        st.session_state.last_job_id = manifest["id"]
        if not jobs.busy:
            status = st.empty()
            manifest = jobs.resume(principal, manifest["id"], progress=status.info)
        st.success(
            f"Job saved: {manifest['status']}. Open Job History for progress, resume, and downloads."
        )
        st.link_button("Open Drive job folder", folder_link(manifest["folder_id"]))


def characters_page(principal, library):
    page_header(
        "Characters",
        "Your cast, ready for the next story. Keep reference images and character descriptions together.",
    )
    archived = st.checkbox("Include archived characters", value=False)
    characters = library.list(principal, include_archived=archived)
    st.caption(
        f"{len(characters)} characters shown · "
        f"{sum(c.status == 'active' for c in characters)} active · "
        f"{sum(c.status == 'archived' for c in characters)} archived"
    )
    if not characters:
        empty_state(
            "No characters to show",
            "Add your first character below or include archived characters to restore a saved reference."
            if principal.role == "admin"
            else "Include archived characters to view saved references, or ask the admin to add an active character.",
        )
    for start in range(0, len(characters), 3):
        for column, character in zip(st.columns(3), characters[start : start + 3], strict=False):
            with column, st.container(border=True, key=f"studio-character-library-{character.id}"):
                character_card(principal, library, character)
                if principal.role == "admin":
                    prefix = f"{character.id}-{character.revision}"
                    with st.expander("Edit character"), st.form(f"edit-{prefix}"):
                        fields, image = character_form(prefix, character)
                        save = st.form_submit_button("Save character", type="primary")
                    if save:
                        library.update(
                            principal,
                            character.id,
                            character.revision,
                            CharacterFields(**fields),
                            image.getvalue() if image else None,
                        )
                        st.rerun()
                    status = "archived" if character.status == "active" else "active"
                    if st.button(
                        "Archive" if status == "archived" else "Restore", key=f"status-{prefix}"
                    ):
                        library.set_status(principal, character.id, character.revision, status)
                        st.rerun()
                    with st.expander("Delete character"), st.form(f"delete-{prefix}"):
                        st.caption(
                            "Permanent deletion removes the library character. Job snapshots and outputs remain available."
                        )
                        confirmation = st.text_input(
                            f"Type {character.slug} to confirm deletion", key=f"confirm-{prefix}"
                        )
                        delete = st.form_submit_button("Permanently delete")
                    if delete:
                        library.delete(principal, character.id, character.revision, confirmation)
                        st.rerun()
    if principal.role == "admin":
        with (
            st.expander("Add character", expanded=not characters),
            st.form("add-character", clear_on_submit=True),
        ):
            fields, image = character_form("new")
            slug = st.text_input(
                "Filename slug (optional)",
                help="Immutable after creation; English letters and numbers are safest.",
            )
            add = st.form_submit_button("Add character", type="primary")
        if add:
            if image is None:
                raise ValueError("Provide a reference image.")
            library.add(principal, CharacterFields(**fields), image.getvalue(), slug=slug or None)
            st.rerun()
    else:
        st.caption("The admin manages character descriptions and reference images.")


def history_page(principal, jobs):
    page_header(
        "Job History",
        "Your shared output library. Both accounts can resume jobs and download completed characters.",
    )
    st.caption("After a restart, press Resume; processing does not restart automatically.")
    if st.button("Refresh history"):
        st.rerun()
    history = jobs.history(principal)
    if not history:
        empty_state(
            "No jobs yet",
            "Open Generate to upload a prompt PDF, choose your cast, and save your first job.",
        )
        return
    for manifest in history:
        complete, failed, total = job_counts(manifest)
        with st.container(border=True, key=f"studio-job-{manifest['id']}"):
            st.subheader(manifest["folder_name"])
            with st.container(horizontal=True):
                status_badge(manifest["status"])
                st.write(f"{complete} of {total} characters complete")
                if failed:
                    st.badge(f"{failed} failed", color="red")
            st.progress(complete / total if total else 0)
            job_details(principal, jobs, manifest)


def job_details(principal, jobs, manifest):
    """Keep history actions and technical reports within each job card."""
    complete, _, _ = job_counts(manifest)
    with st.expander(
        f"{manifest['folder_name']} · {manifest['status']} · Details and downloads",
        expanded=manifest["id"] == st.session_state.get("last_job_id"),
    ):
        job_id = manifest["id"]
        st.caption(
            f"Created by {manifest['creator']} · {manifest['created_at']} · Model: {manifest['model_settings']['claude_model']}"
        )
        st.dataframe(
            [
                {
                    "character": c["snapshot"]["display_name"],
                    "version": c["snapshot"]["version"],
                    "status": c["status"],
                    "stage": c["stage"],
                    "attempts": len(c["attempts"]),
                }
                for c in manifest["characters"]
            ],
            hide_index=True,
            width="stretch",
        )
        downloads = st.session_state.get("job_downloads", {})
        with st.container(horizontal=True):
            resume = manifest["status"] != "complete" and st.button(
                "Resume job", key=f"resume-{job_id}", disabled=jobs.busy
            )
            st.link_button("Open Drive folder", folder_link(manifest["folder_id"]))
            prepare = complete > 0 and st.button(
                "Prepare downloads", key=f"prepare-downloads-{job_id}"
            )
        if resume:
            status = st.empty()
            jobs.resume(principal, job_id, progress=status.info)
            st.rerun()
        if prepare:
            files, bundle = {}, None
            if manifest["status"] == "complete":
                bundle = jobs.bundle(principal, job_id)
                with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
                    for entry in manifest["characters"]:
                        files[entry["snapshot"]["id"]] = {
                            kind: (entry[kind]["name"], archive.read(entry[kind]["name"]))
                            for kind in ["pdf", "png"]
                        }
            else:
                for entry in manifest["characters"]:
                    if entry["status"] == "complete":
                        files[entry["snapshot"]["id"]] = {
                            kind: jobs.download(principal, job_id, entry["snapshot"]["id"], kind)
                            for kind in ["pdf", "png"]
                        }
            downloads = {
                "id": job_id,
                "updated_at": manifest["updated_at"],
                "files": files,
                "zip": bundle,
            }
            st.session_state.job_downloads = downloads
        downloads_ready = (
            downloads.get("id") == job_id and downloads.get("updated_at") == manifest["updated_at"]
        )
        for entry in manifest["characters"]:
            char_id = entry["snapshot"]["id"]
            st.divider()
            st.subheader(entry["snapshot"]["display_name"])
            with st.container(horizontal=True):
                status_badge(entry["status"])
                st.caption(f"Version {entry['snapshot']['version']} · Stage: {entry['stage']}")
                if entry["status"] == "failed" and st.button(
                    "Retry character", key=f"retry-{job_id}-{char_id}", disabled=jobs.busy
                ):
                    status = st.empty()
                    jobs.resume(
                        principal, job_id, character_id=char_id, retry=True, progress=status.info
                    )
                    st.rerun()
                if entry["status"] == "complete" and downloads_ready:
                    for kind, mime in [("pdf", "application/pdf"), ("png", "image/png")]:
                        name, data = downloads["files"][char_id][kind]
                        st.download_button(
                            f"Download {kind.upper()}",
                            data,
                            file_name=name,
                            mime=mime,
                            key=f"download-{job_id}-{char_id}-{kind}",
                        )
            if entry.get("error"):
                st.warning(entry["error"])
            if entry["validation"]:
                with st.expander("Structural validation and AI review"):
                    st.json(entry["validation"])
                    st.caption(
                        "AI review is a separate check. Human semantic review is required before production use."
                    )
            if entry["usage"]:
                with st.expander("Recorded API usage"):
                    st.json(
                        [
                            {key: value for key, value in item.items() if key != "request_id"}
                            for item in entry["usage"]
                        ]
                    )
        if manifest["status"] == "complete" and downloads_ready:
            st.download_button(
                "Download complete job ZIP",
                downloads["zip"],
                file_name=f"{manifest['folder_name']}.zip",
                mime="application/zip",
                key=f"zip-{job_id}",
            )


def main():
    try:
        values = configuration()
    except GenerationError as exc:
        st.title("Character Prompt Studio")
        st.error(str(exc))
        return
    if not values.get("APP_USERS_JSON"):
        st.title("Character Prompt Studio")
        st.info(
            "Configure one admin and one creator in APP_USERS_JSON in deployment Secrets. See README.md for the local password-hash helper."
        )
        return
    try:
        accounts = Accounts(values["APP_USERS_JSON"])
        principal = login(accounts)
        with st.sidebar, st.container(key="studio-sidebar"):
            st.badge("Character studio", color="violet")
            st.subheader("Character Prompt Studio")
            st.caption("Your cast. Every story.")
            st.divider()
            pages = ["Generate", "Characters", "Job History"]
            if principal.role == "admin":
                pages.append("Account status")
            page = st.radio("Page", pages)
            st.divider()
            st.caption("Signed in as")
            st.text(principal.username)
            st.badge(
                principal.role.capitalize(), color="violet" if principal.role == "admin" else "gray"
            )
            if st.button("Sign out", width="stretch"):
                sign_out(st.session_state)
                st.rerun()
            st.caption("Shared jobs · Private Google Drive storage · One job at a time")
        if page == "Account status":
            page_header(
                "Account status",
                "Review workspace access and the connections that power your studio.",
            )
            with st.container(border=True, key="studio-accounts"):
                st.subheader("Workspace accounts")
                st.dataframe(
                    accounts.status(principal),
                    column_config={
                        "username": "Username",
                        "role": "Role",
                        "enabled": "Enabled",
                        "password_configured": "Password set",
                    },
                    hide_index=True,
                    width="stretch",
                )
            with st.container(border=True, key="studio-configuration"):
                st.subheader("Service connections")
                configuration_indicators(values)
            st.caption(
                "Change usernames, hashes, enabled status, and roles through deployment Secrets. Restart the app after changing Secrets. Password hashes and credentials are never displayed here."
            )
            return
        library, jobs = services(Settings.from_mapping(values), values["APP_USERS_JSON"])
        if page == "Generate":
            generate_page(principal, library, jobs)
        elif page == "Characters":
            characters_page(principal, library)
        else:
            history_page(principal, jobs)
    except Exception as exc:  # noqa: BLE001 — safe public boundary.
        st.error(safe_error(exc))


apply_styles()
with st.container(key="studio"):
    main()
