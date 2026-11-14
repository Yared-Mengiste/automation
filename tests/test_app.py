import io
import json
import zipfile
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from prompt_studio import pipeline, storage
from prompt_studio.export import render_sections
from prompt_studio.models import DocumentResponse

APP = str(Path(__file__).resolve().parents[1] / "app.py")


@pytest.fixture
def app_config(monkeypatch, accounts, drive):
    st.cache_resource.clear()
    monkeypatch.setenv("APP_USERS_JSON", json.dumps(accounts.users))
    monkeypatch.setattr(storage, "DriveStorage", lambda settings: drive)
    return accounts


def signed_in(username, password):
    app = AppTest.from_file(APP).run(timeout=30)
    assert not app.exception
    assert not app.get("file_uploader")
    app.text_input[0].set_value(username)
    app.text_input[1].set_value(password)
    app.button[0].click().run(timeout=30)
    assert not app.exception
    assert app.session_state["principal"].username == username
    return app


def test_unconfigured_app_boots_safely(monkeypatch):
    monkeypatch.setenv("APP_USERS_JSON", "")
    app = AppTest.from_file(APP).run(timeout=30)
    assert not app.exception
    assert app.title[0].value == "Character Prompt Studio"
    assert not app.get("file_uploader")
    assert "Configure one admin" in app.info[0].value


def test_hashed_sign_in_and_sign_out(app_config, character):
    app = signed_in("admin", "admin-test-password")
    assert len(app.get("file_uploader")) == 1
    next(button for button in app.button if button.label == "Sign out").click().run(timeout=30)
    assert not app.exception
    assert not app.get("file_uploader")
    assert len(app.text_input) == 2
    assert "principal" not in app.session_state


def test_generate_preview_accepts_arbitrary_pdf(app_config, monkeypatch, character):
    upload = io.BytesIO(
        render_sections(DocumentResponse(content="Recipe notes without shots."), "Notes")
    )
    upload.name = "notes.pdf"
    app = signed_in("creator", "creator-test-password")
    monkeypatch.setattr(st, "file_uploader", lambda *args, **kwargs: upload)
    app.multiselect[0].set_value([character.id]).run(timeout=30)
    assert not app.exception
    assert any(item.label == "PDF pages" and item.value == "1" for item in app.metric)
    assert any("PDF is ready" in item.value for item in app.success)
    assert any(button.label == "Save and generate" for button in app.button)


def test_creator_library_is_read_only(app_config, character):
    app = signed_in("creator", "creator-test-password")
    assert "Account status" not in app.radio[0].options
    app.radio[0].set_value("Characters").run(timeout=30)
    assert not app.exception
    assert not app.get("file_uploader")
    assert not any(
        button.label in {"Add character", "Save character", "Archive", "Permanently delete"}
        for button in app.button
    )


def test_admin_account_page_never_displays_hashes(app_config):
    app = signed_in("admin", "admin-test-password")
    app.radio[0].set_value("Account status").run(timeout=30)
    assert not app.exception
    assert "password_hash" not in str(app.dataframe[0].value)
    assert list(app.dataframe[0].value["username"]) == ["admin", "creator"]
    assert not app.get("file_uploader")


def test_two_sessions_share_durable_history(app_config, jobs, creator, character, pdf):
    job = jobs.create(creator, pdf, "Shared.pdf", [character])
    jobs.resume(creator, job["id"])
    for username, password in [
        ("admin", "admin-test-password"),
        ("creator", "creator-test-password"),
    ]:
        app = signed_in(username, password)
        app.radio[0].set_value("Job History").run(timeout=30)
        assert not app.exception
        assert any("shared_" in expander.label for expander in app.expander)
        assert not app.get("download_button")
        next(button for button in app.button if button.label == "Prepare downloads").click().run(
            timeout=30
        )
        assert not app.exception
        assert len(app.get("download_button")) == 3
        downloads = app.session_state["job_downloads"]
        assert downloads["files"][character.id]["pdf"][1].startswith(b"%PDF")
        assert downloads["files"][character.id]["png"][1].startswith(b"\x89PNG")
        assert zipfile.is_zipfile(io.BytesIO(downloads["zip"]))


def test_admin_edits_archives_and_restores_from_gallery(app_config, library, admin, character):
    app = signed_in("admin", "admin-test-password")
    app.radio[0].set_value("Characters").run(timeout=30)
    assert not app.exception
    prefix = f"{character.id}-{character.revision}"
    app.text_area(key=f"{prefix}-voice").set_value("A warm, clear studio voice")
    next(button for button in app.button if button.label == "Save character").click().run(
        timeout=30
    )
    assert not app.exception
    updated = library.get(admin, character.id)
    assert updated.voice == "A warm, clear studio voice"
    assert updated.version == character.version + 1
    next(button for button in app.button if button.label == "Archive").click().run(timeout=30)
    assert not app.exception
    assert library.get(admin, character.id).status == "archived"
    assert not app.get("imgs")
    app.checkbox[0].check().run(timeout=30)
    next(button for button in app.button if button.label == "Restore").click().run(timeout=30)
    assert not app.exception
    assert library.get(admin, character.id).status == "active"


def test_empty_library_and_history_explain_next_action(app_config):
    app = signed_in("creator", "creator-test-password")
    assert "Open Characters" in app.info[0].value
    app.radio[0].set_value("Characters").run(timeout=30)
    assert not app.exception
    assert "ask the admin" in app.info[0].value
    app.radio[0].set_value("Job History").run(timeout=30)
    assert not app.exception
    assert "Open Generate" in app.info[0].value


def test_missing_configuration_shows_presence_only(app_config, monkeypatch):
    for name in [
        "ANTHROPIC_API_KEY",
        "GOOGLE_CLIENT_ID",
        "GOOGLE_CLIENT_SECRET",
        "GOOGLE_REFRESH_TOKEN",
        "DRIVE_ROOT_ID",
    ]:
        monkeypatch.setenv(name, "")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "secret-must-not-be-rendered")
    app = signed_in("admin", "admin-test-password")
    app.radio[0].set_value("Account status").run(timeout=30)
    assert not app.exception
    assert not app.json
    assert "secret-must-not-be-rendered" not in str(app)
    assert sum(item.value == ":orange-badge[Missing]" for item in app.markdown) == 4
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-must-not-be-rendered")
    app.run(timeout=30)
    assert not app.exception
    assert sum(item.value == ":green-badge[Configured]" for item in app.markdown) == 2
    assert "client-must-not-be-rendered" not in str(app)


def test_partial_job_downloads_retry_and_stale_preparation(
    app_config, monkeypatch, jobs, library, admin, creator, character, fields, png, pdf, ai
):
    second = library.add(
        admin, fields.model_copy(update={"display_name": "Sara", "mapping": "@image_1 = Sara"}), png
    )
    job = jobs.create(creator, pdf, "Partial.pdf", [character, second])
    ai.bad_edits = True
    jobs.resume(creator, job["id"], character_id=second.id)
    ai.bad_edits = False
    jobs.resume(creator, job["id"], character_id=character.id)
    monkeypatch.setattr(pipeline, "JobService", lambda *args, **kwargs: jobs)
    app = signed_in("creator", "creator-test-password")
    app.radio[0].set_value("Job History").run(timeout=30)
    assert not app.exception
    assert any("1 of 2 characters complete" in item.value for item in app.markdown)
    next(button for button in app.button if button.label == "Prepare downloads").click().run(
        timeout=30
    )
    assert not app.exception
    assert len(app.get("download_button")) == 2
    assert app.session_state["job_downloads"]["zip"] is None
    next(button for button in app.button if button.label == "Retry character").click().run(
        timeout=30
    )
    assert not app.exception
    assert jobs.history(creator)[0]["status"] == "complete"
    assert not app.get("download_button")
    next(button for button in app.button if button.label == "Prepare downloads").click().run(
        timeout=30
    )
    assert not app.exception
    assert len(app.get("download_button")) == 5


def test_pending_job_resume_and_busy_controls(
    app_config, monkeypatch, jobs, creator, character, pdf
):
    job = jobs.create(creator, pdf, "Pending.pdf", [character])
    monkeypatch.setattr(pipeline, "JobService", lambda *args, **kwargs: jobs)
    app = signed_in("creator", "creator-test-password")
    jobs.gate.acquire()
    try:
        app.radio[0].set_value("Job History").run(timeout=30)
        assert not app.exception
        assert next(button for button in app.button if button.label == "Resume job").disabled
        assert not any(button.label == "Prepare downloads" for button in app.button)
    finally:
        jobs.gate.release()
    app.run(timeout=30)
    next(button for button in app.button if button.label == "Resume job").click().run(timeout=30)
    assert not app.exception
    assert jobs.history(creator)[0]["id"] == job["id"]
    assert jobs.history(creator)[0]["status"] == "complete"
