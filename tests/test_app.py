import json
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from prompt_studio import storage

APP = str(Path(__file__).resolve().parents[1] / "app.py")


@pytest.fixture
def app_config(monkeypatch, accounts, drive):
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
