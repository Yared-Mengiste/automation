import json
import sys

from prompt_studio import __main__ as cli


def setup(monkeypatch, accounts, password, args):
    monkeypatch.setenv("APP_USERS_JSON", json.dumps(accounts.users))
    monkeypatch.setattr(cli.getpass, "getpass", lambda _: password)
    monkeypatch.setattr(sys, "argv", ["prompt_studio", "--username", "creator", *args])


def test_cli_free_check_uses_hashed_auth_without_drive(
    monkeypatch, accounts, pdf, tmp_path, capsys
):
    path = tmp_path / "cake.pdf"
    path.write_bytes(pdf)
    setup(monkeypatch, accounts, "creator-test-password", ["check", str(path)])
    assert cli.main() == 0
    output = json.loads(capsys.readouterr().out)
    assert [(section["duration"], section["shots"]) for section in output["sections"]] == [
        (14, 8),
        (22, 4),
    ]


def test_cli_invalid_password_blocks_check(monkeypatch, accounts, pdf, tmp_path, capsys):
    path = tmp_path / "cake.pdf"
    path.write_bytes(pdf)
    setup(monkeypatch, accounts, "bad-password", ["check", str(path)])
    assert cli.main() == 1
    assert "Invalid credentials" in capsys.readouterr().err


def test_cli_generates_and_downloads_shared_job(
    monkeypatch, accounts, drive, jobs, character, pdf, tmp_path, capsys
):
    path = tmp_path / "cake.pdf"
    path.write_bytes(pdf)
    setup(
        monkeypatch,
        accounts,
        "creator-test-password",
        [
            "generate",
            str(path),
            "--character",
            character.slug,
            "--output",
            str(tmp_path / "outputs"),
        ],
    )
    monkeypatch.setattr(cli, "DriveStorage", lambda _: drive)
    monkeypatch.setattr(cli, "JobService", lambda *args: jobs)
    assert cli.main() == 0
    assert len(list((tmp_path / "outputs").glob("*.zip"))) == 1
    assert "Job " in capsys.readouterr().out


def test_cli_resume_reuses_complete_ai_work(
    monkeypatch, accounts, drive, jobs, character, creator, pdf, ai
):
    job = jobs.create(creator, pdf, "cake.pdf", [character])
    jobs.resume(creator, job["id"])
    setup(monkeypatch, accounts, "creator-test-password", ["resume", job["id"]])
    monkeypatch.setattr(cli, "DriveStorage", lambda _: drive)
    monkeypatch.setattr(cli, "JobService", lambda *args: jobs)
    assert cli.main() == 0
    assert (ai.rewrites, ai.reviews) == (1, 1)


def test_local_hash_helper_never_prints_password(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["prompt_studio", "hash-password"])
    monkeypatch.setattr(cli.getpass, "getpass", lambda _: "private-test-password")
    assert cli.main() == 0
    output = capsys.readouterr().out
    assert output.startswith("scrypt$")
    assert "private-test-password" not in output
