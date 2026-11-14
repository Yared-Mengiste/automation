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
    with st.form("sign-in"):
        username = st.text_input("Username", max_chars=100)
        password = st.text_input("Password", type="password", max_chars=1024)
        submitted = st.form_submit_button("Sign in", type="primary")
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
    for name, maximum in limits.items():
        fields[name] = st.text_area(
            labels[name], value=values.get(name, ""), max_chars=maximum, key=f"{prefix}-{name}"
        )
    image = st.file_uploader(
        "Replace reference image" if current else "Reference image",
        type=["png", "jpg", "jpeg", "webp"],
        key=f"{prefix}-image",
    )
    return fields, image


def generate_page(principal, library, jobs):
    st.subheader("Generate")
    st.write(
        "Replace the primary character in an existing prompt PDF. Every section keeps its own timeline and dialogue."
    )
    active = library.list(principal)
    if not active:
        st.info("The admin needs to add an active character before you can generate.")
        return
    upload = st.file_uploader("Original prompt PDF", type=["pdf"])
    selected_ids = st.multiselect(
        "Characters",
        options=[c.id for c in active],
        format_func=lambda key: next(
            f"{c.display_name} · v{c.version}" for c in active if c.id == key
        ),
    )
    selected = [c for c in active if c.id in selected_ids]
    if upload is None or not selected:
        st.caption(
            "Limits: 10 MB, 30 text-based pages, 40,000 extracted characters, @image_1 only."
        )
        return
    # Free preview is rebuilt from current inputs and versions; stale previews cannot start paid work.
    source, slug, checked = jobs.check(principal, upload.getvalue(), upload.name, selected)
    st.success("PDF structure passed. This preview uses no AI calls.")
    st.dataframe(checked["sections"], hide_index=True, width="stretch")
    st.dataframe(checked["characters"], hide_index=True, width="stretch")
    for character in selected:
        with st.expander(f"{character.display_name} · version {character.version}"):
            st.image(library.image(principal, character), width=240)
            st.code(character.mapping, language=None)
            st.json(
                {
                    key: getattr(character, key)
                    for key in [
                        "pronouns",
                        "voice",
                        "personality",
                        "wardrobe",
                        "negatives",
                        "consistency",
                    ]
                }
            )
    with st.expander("Extracted prompts", expanded=False):
        st.code(checked["extracted_prompts"], language=None, wrap_lines=True)
    st.write(checked["rules"])
    st.caption(
        f"Generate makes paid Claude calls using {jobs.settings.claude_model}. The prompt and selected character snapshots are sent to Anthropic and saved in the private Drive folder."
    )
    if jobs.busy:
        st.info("Another job is processing. Your new job will be saved as pending.")
    if st.button("Save and generate", type="primary"):
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
    st.subheader("Characters")
    archived = st.checkbox("Include archived characters", value=False)
    characters = library.list(principal, include_archived=archived)
    for character in characters:
        with st.expander(f"{character.display_name} · v{character.version} · {character.status}"):
            st.image(library.image(principal, character), width=260)
            st.caption(f"Filename slug: {character.slug} · ID: {character.id}")
            st.code(character.mapping, language=None)
            st.json(
                {
                    key: getattr(character, key)
                    for key in [
                        "pronouns",
                        "voice",
                        "personality",
                        "wardrobe",
                        "negatives",
                        "consistency",
                    ]
                }
            )
            if principal.role == "admin":
                prefix = f"{character.id}-{character.revision}"
                with st.form(f"edit-{prefix}"):
                    fields, image = character_form(prefix, character)
                    save = st.form_submit_button("Save character")
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
                with st.form(f"delete-{prefix}"):
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
    st.subheader("Job History")
    st.caption(
        "Both accounts share these jobs. After a restart, press Resume; processing does not restart automatically."
    )
    if st.button("Refresh history"):
        st.rerun()
    history = jobs.history(principal)
    if not history:
        st.info("No jobs yet.")
    for manifest in history:
        with st.expander(
            f"{manifest['folder_name']} · {manifest['status']}",
            expanded=manifest["id"] == st.session_state.get("last_job_id"),
        ):
            job_id = manifest["id"]
            st.caption(
                f"Created by {manifest['creator']} · {manifest['created_at']} · Model: {manifest['model_settings']['claude_model']}"
            )
            st.link_button("Open Drive folder", folder_link(manifest["folder_id"]))
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
            if any(c["status"] == "complete" for c in manifest["characters"]) and st.button(
                "Prepare downloads", key=f"prepare-downloads-{job_id}"
            ):
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
                                kind: jobs.download(
                                    principal, job_id, entry["snapshot"]["id"], kind
                                )
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
                downloads.get("id") == job_id
                and downloads.get("updated_at") == manifest["updated_at"]
            )
            if manifest["status"] != "complete" and st.button(
                "Resume job", key=f"resume-{job_id}", disabled=jobs.busy
            ):
                status = st.empty()
                jobs.resume(principal, job_id, progress=status.info)
                st.rerun()
            for entry in manifest["characters"]:
                char_id = entry["snapshot"]["id"]
                st.write(f"**{entry['snapshot']['display_name']}**")
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
            if manifest["status"] == "complete" and downloads_ready:
                st.download_button(
                    "Download complete job ZIP",
                    downloads["zip"],
                    file_name=f"{manifest['folder_name']}.zip",
                    mime="application/zip",
                    key=f"zip-{job_id}",
                )


def main():
    st.title("Character Prompt Studio")
    try:
        values = configuration()
    except GenerationError as exc:
        st.error(str(exc))
        return
    if not values.get("APP_USERS_JSON"):
        st.info(
            "Configure one admin and one creator in APP_USERS_JSON in deployment Secrets. See README.md for the local password-hash helper."
        )
        return
    try:
        accounts = Accounts(values["APP_USERS_JSON"])
        principal = login(accounts)
        with st.sidebar:
            st.write(f"Signed in as **{principal.username}** · {principal.role}")
            if st.button("Sign out"):
                sign_out(st.session_state)
                st.rerun()
            pages = ["Generate", "Characters", "Job History"]
            if principal.role == "admin":
                pages.append("Account status")
            page = st.radio("Page", pages)
            st.caption("Shared jobs · Private Google Drive storage · One job at a time")
        if page == "Account status":
            st.subheader("Account status")
            st.dataframe(accounts.status(principal), hide_index=True, width="stretch")
            st.json(
                {
                    key: bool(values.get(key))
                    for key in [
                        "ANTHROPIC_API_KEY",
                        "GOOGLE_CLIENT_ID",
                        "GOOGLE_CLIENT_SECRET",
                        "GOOGLE_REFRESH_TOKEN",
                        "DRIVE_ROOT_ID",
                    ]
                }
            )
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


main()
