# Character Prompt Studio

A Streamlit app that replaces the primary character in existing text-based prompt PDFs. It saves the character library, original PDFs, immutable job snapshots, progress, and paired PDF/PNG outputs in a private Google Drive folder. Claude proposes targeted character edits and independently reviews the result. Human review is required before production use.

The sample `banana-cake.pdf` contains **14 seconds / 8 shots** and **22 seconds / 4 shots**. Both sections retain their own timeline and dialogue. Personal sample media is excluded from Git.

## Start here

**Streamlit Community Cloud** is the hosting service that turns a GitHub repository into a web app. Your two application accounts are separate from your Google account and your GitHub account.

Before live generation, you need:

1. A GitHub repository containing these application files on branch **`main`**.
2. An Anthropic API account with billing and a key for a Claude model supporting structured output.
3. A dedicated Google account, a Google Cloud project with the Drive API enabled, and offline OAuth credentials.
4. Two password hashes: one for your admin and one for your creator.

Credentials are not configured in this workspace. The app and tests can run locally, but paid generation and live Drive access require these settings. Do not paste keys, passwords, or refresh tokens into chat or commit them to Git.

## Local setup

Use Python 3.12 (Python 3.11+ is required):

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
# Debian / Ubuntu:
sudo apt-get install fonts-dejavu-core fonts-noto-core
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
python -m prompt_studio hash-password
```

Run the hash command twice, choosing different passwords of at least 12 characters. Put the resulting hashes into the `password_hash` fields in `APP_USERS_JSON` in your local Secrets file. Never put plaintext passwords in that setting. All accounts require sign-in; there is no unauthenticated mode.

`APP_USERS_JSON` must contain exactly one admin and one creator, each with `password_hash`, `role`, and boolean `enabled`. Change names, passwords, enabled flags, and roles in Secrets and restart the app. The admin's Account Status page displays configuration status without revealing hashes or credentials. Password and role changes invalidate existing sessions. Sign out clears session state.

The inherited default is `CLAUDE_MODEL=claude-sonnet-5-5`. Set this to a structured-output model available in your Anthropic account before acceptance testing. Model access has not been verified with live credentials.

## Connect Google Drive once

1. Sign into your dedicated Google account and create a project in [Google Cloud Console](https://console.cloud.google.com/).
2. Enable **Google Drive API**. Configure Google Auth Platform branding, audience, and data access. Request only `https://www.googleapis.com/auth/drive.file`.
3. For an external audience, change publishing status from **Testing** to **In production** before issuing the token used for deployment. Google documents that external Testing refresh tokens expire after seven days when requesting Drive access. Follow any branding or verification requirements shown in your console. An eligible Workspace internal audience is another option.
4. Create an **OAuth client ID → Desktop app**, and download its client JSON to a private local file such as `google-client.json`.
5. On your own computer, run:

```sh
python scripts/connect_google.py google-client.json
```

The helper opens your browser for consent, requests offline access, and saves the client ID, client secret, and refresh token in a new `.streamlit/google-oauth.toml` file with mode `0600`. It never prints tokens. Copy those three entries into your local or deployment Secrets; keep the generated file private. It will not overwrite an existing credentials file. The helper runs locally; the hosted app needs no OAuth callback page.

Leave `DRIVE_ROOT_ID` empty on the first launch. The app creates a private **Character Prompt Studio** folder. Reuse the same OAuth client and set `DRIVE_ROOT_ID` to that folder's ID if you need to pin it across deployments. The app rejects arbitrary folders and files outside its managed set. It never creates public sharing permissions.

Drive folder links require a Google login that has access to the private folder, normally the dedicated owner account. Application username/password accounts do not automatically grant Google Drive browser access. If you need individual Google logins, share the private root with those specific accounts using Drive's own interface.

If credentials are revoked or expire, reconnect with offline consent, replace `GOOGLE_REFRESH_TOKEN` in Secrets, restart, and resume the job. Keep the OAuth app in production; refresh tokens can still be revoked or expire for other reasons.

Official documentation checked on 2026-11-13:

- [Google OAuth offline access](https://developers.google.com/identity/protocols/oauth2/web-server#offline)
- [Refresh-token expiration, including Testing's seven-day limit](https://developers.google.com/identity/protocols/oauth2#expiration)
- [Drive scopes and the recommended per-file scope](https://developers.google.com/workspace/drive/api/guides/api-specific-auth)

## Run locally or deploy on main

```sh
streamlit run app.py
```

Open the URL printed by Streamlit. Sign in as the admin to add character descriptions and references; either account can generate and resume shared jobs.

For Community Cloud:

1. Push the application files to your GitHub repository's `main` branch. Include `app.py`, `prompt_studio/`, `requirements.txt`, `packages.txt`, and `.streamlit/config.toml`. Exclude Secrets, client JSON, personal media, and generated outputs.
2. Sign into [Streamlit Community Cloud](https://share.streamlit.io/) using GitHub. Choose **Create app**, select your repository, branch **main**, and entrypoint **app.py**.
3. In advanced settings, choose **Python 3.12** and paste the completed contents of `.streamlit/secrets.toml` into the Secrets field. Use your real hashes and credentials.
4. Deploy **one instance**. Community Cloud installs Python dependencies from `requirements.txt` and Debian font packages from `packages.txt`.
5. Open the hosted URL, sign in, and check admin Account Status. Add the characters and run the acceptance steps below.

See [deployment](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app), [dependencies](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/app-dependencies), and [Secrets management](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/secrets-management). These official deployment/dependency pages were reachable during implementation; publishing has not been performed.

Docker uses the same font packages and services, without FFmpeg:

```sh
cp .env.example .env
# Fill .env with hashes, model setting, API key, and Google OAuth credentials.
docker compose up --build
```

## Use the app

- **Generate:** upload a PDF and select active characters. The free preview shows all extracted prompts, original mappings, independent section durations and shot counts, character versions, exact target mappings, attachment names, and replacement rules. **Save and generate** snapshots inputs before making paid calls. If processing is busy, it saves a pending job.
- **Characters:** everyone can preview active characters and saved rules. The admin can add/edit descriptions, replace PNG/JPEG/WebP references (normalized to PNG), archive, restore, and permanently delete after typing the immutable slug. Names and sanitized slugs must be unique, including archived entries. Description/image changes increment the version; stale edits are rejected.
- **Job History:** both accounts see all shared jobs. Resume unfinished stages, retry a failed character, inspect structural and AI reports separately, press **Prepare downloads** to load complete PDF/PNG pairs and a complete-job ZIP, and open Drive folders. Files are fetched on request so history refreshes do not download every past job.
- **Account Status:** admin-only configuration status; edit account settings through Secrets.

A character has an immutable ID and filename slug, display name, one exact `@image_1 = ...` mapping line, reference PNG, pronouns, voice/accent, personality, wardrobe, negatives, consistency instructions, active/archived status, version, revision, and UTC timestamps. All descriptions are required. Unsupported Unicode glyphs are rejected before paid work or rendering; deployed fonts include Ethiopic.

## Supported PDFs and replacement rules

Limits are **10 MB**, **30 pages**, and **40,000 extracted characters**. PDFs must contain readable text on every page, consistent repeated `@image_1` mappings, `Reference Mapping:` section starts, `FORMAT: 14s / 8 SHOTS / ...` declarations, and timed `SHOT 1 — 0:00 to 0:02.4, ...` blocks. Each section must start at zero and cover its declared duration without gaps, overlaps, or misnumbered shots. Quoted speaker labels and `DIALOGUE:` blocks are preserved. Unsupported, scanned, encrypted, inconsistent, or ambiguous PDFs are rejected before paid calls. Extraction repairs layout whitespace and fragmented labels while joining page-spanning blocks.

The mapping is inserted exactly. The matching output PNG filename goes on a separate `ATTACHMENT:` line. Claude may edit only character-dependent appearance, pronouns, wardrobe, voice/accent, personality, negatives, and continuity. Saved traits win over conflicting original traits; shared traits remain. Story, actions, camera directions, locations, lighting, style, sound, and dialogue content must be preserved. Shot headers, timing declarations, order, and dialogue are protected by deterministic validation. Edits must use exact unique source substrings and cannot overlap.

An independent AI review checks obsolete or missing character traits and changes to protected meaning. One corrective rewrite is allowed, followed by repeated structural validation and AI review. If validation still fails, that character gets no completed output. AI review is fallible: **human semantic review is required before production**.

## Durable files and resume behavior

```text
Character Prompt Studio/
  Characters/<slug>/
    character.json
    reference.png
  Jobs/<Addis-Ababa-date>_<prompt-slug>_<short-job-id>/
    <prompt-slug>-original.pdf
    job-manifest.json
    Snapshots/parsed-source.json, <character-slug>.json, <character-slug>.png
    Checkpoints/<character>-attempt-<n>-round-<n>-<stage>.json
    <character-slug>-<prompt-slug>.pdf
    <character-slug>-<prompt-slug>.png
```

The manifest records creator, UTC timestamps, source, snapshots/versions, model settings, attempts, stage/status, structural/AI reports, token usage, provider request IDs, and reserved Drive file IDs. The initial manifest briefly embeds input bytes until snapshot uploads finish, making interrupted job creation recoverable without relying on later library contents. It never stores deployment credentials.

Rewrite, candidate, review, and validated-text checkpoints have attempt-specific reserved IDs. Checkpoints are reconciled even when an upload finishes but the manifest update is interrupted. Validated text is saved before PDF rendering. PDFs and PNGs are uploaded immediately using pre-reserved IDs and checksums. Completion requires both files. Resume skips completed characters and checkpointed AI work. An AI response interrupted **before** its checkpoint may require another paid call. Explicit retries after exhausted validation create a fresh attempt; upload/render retries reuse the validated attempt.

Library edits, archiving, image replacement, and deletion do not alter snapshots or old outputs. During an interrupted image edit, the character document continues to point to its existing reference until the new image is uploaded. A short-lived extra reference may remain after an interruption; the document's image ID is authoritative.

There is **no background worker**: restart requires a user to press Resume. Run one application instance against a root. The CLI must not run concurrently with a hosted instance against the same root; the gate is process-local, not a distributed lock. Complete-job ZIPs are built from durable original/output files and validation reports, excluding internal checkpoints and snapshots. Private logs contain IDs, stages, error categories, and provider request IDs, excluding raw provider bodies, credentials, passwords, and prompt contents.

## CLI

Use the same Secrets and two account passwords. Passwords are prompted securely, not supplied in command arguments:

```sh
python -m prompt_studio --username creator check banana-cake.pdf
python -m prompt_studio --username creator generate banana-cake.pdf --character maya --character sara --output outputs
python -m prompt_studio --username admin resume JOB_UUID --output outputs
python -m prompt_studio --username creator resume JOB_UUID --character-id CHARACTER_UUID --retry
```

`check` is free and requires account validation but no Drive or AI credentials. Generation prints the free preview and saves the durable job before processing. Credentials can also come from `.env`; real environment variables override Secrets. API keys are never stored in job snapshots.

## Verification and live acceptance

```sh
pytest -q
ruff check prompt_studio app.py scripts tests
ruff format --check prompt_studio app.py scripts tests
```

Tests use mocked AI and Drive, plus real SDK serialization against local transports. They exercise the sample dialect, whitespace fragmentation, page-spanning dialogue, mappings/filename pairing, protected timings/dialogue, conflicts/shared traits, review correction/exhaustion, hashed authentication and permissions, shared history/sign-out, stale edits, versioning, deletion, Unicode PDF extraction, and restarts/failures before and after every durable stage. They verify that checkpointed AI work and uploaded files are reused. Mocked review tests cannot establish real semantic quality. Server socket binding and Docker daemon access are blocked by this workspace sandbox, so external browser/server and Docker smoke checks must be run after setup; Streamlit app execution is covered by AppTest.

After configuring real credentials, run this acceptance job:

1. Add two real characters with complete descriptions and reference images as admin.
2. Upload `banana-cake.pdf`, select both, and verify the free preview is 14s/8 shots plus 22s/4 shots.
3. Generate and inspect both PDF/PNG pairs. Check exact mappings, matching PNG instructions, unchanged dialogue and all timings; manually review obsolete traits and story preservation.
4. Create another job, interrupt or restart the app after a character finishes, and press Resume from the other account. Verify the completed character is skipped, file IDs stay the same, and remaining work completes.
5. Download all pairs and the ZIP from both app accounts, and open the private Drive folder using an authorized Google login. Confirm ZIP contents and direct Drive access.

Live acceptance is pending until OAuth, API billing/key/model access, and hosting are configured. OCR, multiple reference characters, video analysis/transcription, generated images/video, dollar budgets, and automatic background processing are outside v1.
