"""Authenticated CLI using the same parser, library, and recoverable job service."""

import argparse
import getpass
import json
import os
import sys
import tomllib
from pathlib import Path

from dotenv import load_dotenv

from .auth import Accounts, hash_password
from .characters import CharacterLibrary
from .errors import GenerationError
from .logging_utils import configure_private_logging
from .pdf_source import read_pdf
from .pipeline import JobService
from .settings import Settings
from .storage import DriveStorage, folder_link


def main() -> int:
    configure_private_logging()
    parser = argparse.ArgumentParser(description="Check or replace characters in prompt PDFs.")
    parser.add_argument("--secrets", type=Path, default=Path(".streamlit/secrets.toml"))
    parser.add_argument("--username", help="Configured admin or creator account")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "hash-password", help="Generate a salted scrypt hash locally; no sign-in required"
    )
    check = commands.add_parser("check", help="Free PDF structure check")
    check.add_argument("pdf", type=Path)
    generate = commands.add_parser("generate", help="Save snapshots and generate, or queue if busy")
    generate.add_argument("pdf", type=Path)
    generate.add_argument(
        "--character",
        action="append",
        required=True,
        help="Active character ID or immutable slug; repeat for multiple characters",
    )
    resume = commands.add_parser("resume", help="Resume a shared job using its immutable snapshots")
    resume.add_argument("job_id")
    resume.add_argument("--character-id")
    resume.add_argument(
        "--retry", action="store_true", help="Allow a fresh attempt after exhausted validation"
    )
    for command in [generate, resume]:
        command.add_argument("--output", type=Path, help="Save the complete-job ZIP locally")
    args = parser.parse_args()
    try:
        if args.command == "hash-password":
            password = getpass.getpass("New password (12+ characters): ")
            if password != getpass.getpass("Repeat password: "):
                raise ValueError("Passwords do not match.")
            print(hash_password(password))
            return 0
        load_dotenv()
        values = tomllib.loads(args.secrets.read_text()) if args.secrets.is_file() else {}
        values.update(
            {
                key: value
                for key, value in os.environ.items()
                if key
                in {
                    "APP_USERS_JSON",
                    "ANTHROPIC_API_KEY",
                    "CLAUDE_MODEL",
                    "GOOGLE_CLIENT_ID",
                    "GOOGLE_CLIENT_SECRET",
                    "GOOGLE_REFRESH_TOKEN",
                    "DRIVE_ROOT_ID",
                }
            }
        )
        accounts = Accounts(values.get("APP_USERS_JSON", ""))
        principal = accounts.authenticate(
            args.username or input("Username: "), getpass.getpass("Password: ")
        )
        if not principal:
            raise GenerationError("Invalid credentials or disabled account.")
        settings = Settings.from_mapping(values)
        if args.command == "check":
            source = read_pdf(args.pdf.read_bytes(), settings)
            print(
                json.dumps(
                    {
                        "sections": [
                            {
                                "duration": s.duration,
                                "shots": len(s.shots),
                                "mapping": next(b.text for b in s.blocks if b.kind == "mapping"),
                            }
                            for s in source.sections
                        ],
                        "extracted_prompts": source.text(),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        storage = DriveStorage(settings)
        library = CharacterLibrary(storage, accounts)
        jobs = JobService(storage, accounts, library, settings)
        if args.command == "generate":
            active = library.list(principal)
            selected = []
            for key in args.character:
                char = next((c for c in active if c.id == key or c.slug == key), None)
                if not char:
                    raise ValueError(
                        "An active character with the supplied ID or slug was not found."
                    )
                selected.append(char)
            source, slug, free_preview = jobs.check(
                principal, args.pdf.read_bytes(), args.pdf.name, selected
            )
            print(json.dumps(free_preview, ensure_ascii=False, indent=2))
            manifest = jobs.create(principal, args.pdf.read_bytes(), args.pdf.name, selected)
            print(f"Saved job {manifest['id']}: {folder_link(manifest['folder_id'])}")
            manifest = jobs.resume(principal, manifest["id"], progress=print)
        else:
            manifest = jobs.resume(
                principal,
                args.job_id,
                character_id=args.character_id,
                retry=args.retry,
                progress=print,
            )
        print(f"Job {manifest['id']}: {manifest['status']} · {folder_link(manifest['folder_id'])}")
        if args.output and manifest["status"] == "complete":
            args.output.mkdir(parents=True, exist_ok=True)
            (args.output / f"{manifest['folder_name']}.zip").write_bytes(
                jobs.bundle(principal, manifest["id"])
            )
        return 0 if manifest["status"] in {"complete", "pending"} else 1
    except Exception as exc:  # noqa: BLE001 — do not display raw provider exceptions.
        message = (
            str(exc)
            if isinstance(exc, GenerationError) or type(exc) is ValueError
            else "Check account configuration, input files, and connectivity."
        )
        print(f"Operation failed: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
