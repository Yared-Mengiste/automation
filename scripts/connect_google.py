"""Run once on your own computer to create offline Drive credentials."""

import argparse
import json
import os
import sys
from pathlib import Path

SCOPE = "https://www.googleapis.com/auth/drive.file"


def main():
    parser = argparse.ArgumentParser(
        description="Connect the dedicated Google account using a Desktop OAuth client."
    )
    parser.add_argument(
        "client_json", type=Path, help="OAuth Desktop client JSON downloaded from Google Cloud"
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".streamlit/google-oauth.toml"),
        help="New private file; existing files are never overwritten",
    )
    args = parser.parse_args()
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow

        flow = InstalledAppFlow.from_client_secrets_file(str(args.client_json), scopes=[SCOPE])
        if (
            flow.client_config.get("auth_uri") != "https://accounts.google.com/o/oauth2/auth"
            or flow.client_config.get("token_uri") != "https://oauth2.googleapis.com/token"
        ):
            raise ValueError("Use a genuine Google OAuth Desktop client configuration.")
        credentials = flow.run_local_server(
            host="localhost", port=0, access_type="offline", prompt="consent"
        )
        if not credentials.refresh_token:
            raise ValueError(
                "Google did not issue a refresh token. Revoke the prior app grant and reconnect with offline consent."
            )
        values = {
            "GOOGLE_CLIENT_ID": credentials.client_id,
            "GOOGLE_CLIENT_SECRET": credentials.client_secret,
            "GOOGLE_REFRESH_TOKEN": credentials.refresh_token,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as output:
            output.write(
                "\n".join(f"{key} = {json.dumps(value)}" for key, value in values.items()) + "\n"
            )
        print(
            f"Credentials saved privately to {args.output}. Paste its contents into deployment Secrets; never commit this file."
        )
        return 0
    except Exception as exc:  # noqa: BLE001 — OAuth exceptions may contain tokens.
        message = (
            str(exc)
            if type(exc) is ValueError
            else "Check the Desktop OAuth client, local browser access, output path, and Google consent configuration."
        )
        print(f"Connection failed: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
