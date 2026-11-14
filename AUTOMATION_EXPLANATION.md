# PDF character replacement architecture

`app.py` authenticates the admin or creator and provides Generate, Characters, shared Job History, and admin Account Status. Password hashes and roles live in deployment Secrets. Every service validates the principal; library mutations also require admin privileges.

`pdf_source.py` extracts layout-aware PDF text with `pypdf`, repairs whitespace, joins page-spanning blocks, and parses independent sections. It rejects unsupported structure before paid calls. `models.py` defines the validated source blocks, character versions, exact-span edits, and semantic review responses.

`characters.py` owns the current Google Drive library. IDs and filename slugs do not change; descriptions/images increment versions. Edits require the current revision. An image replacement uploads a new image before publishing its document pointer, preserving a consistent reference through interruptions. Archiving/restoring changes selection visibility. Confirmed deletion removes only the current library folder.

`pipeline.py` snapshots the original, parsed source, selected descriptions/versions, images, and model settings in a recoverable job. The initial manifest embeds input bytes until all snapshot uploads finish. Reserved IDs precede every upload. Attempt-specific checkpoints store the rewrite, validated candidate, independent AI review, and validated final text. Resume reconciles checkpoints with the manifest after interruptions. Completed characters are skipped. Uncheckpointed AI responses may need another paid call.

`agents.py` uses Anthropic's structured-output SDK for small source-identified edits and a separate complete-result review. `replacement.py` applies unique, non-overlapping spans and inserts exact mappings plus paired PNG instructions. It protects section boundaries, shot headers/timing/order, and dialogue. Saved traits override conflicting source traits; shared traits and protected meaning remain. One corrective rewrite is allowed. Structural validation and AI review are reported separately; a human must accept production semantics.

`export.py` uses `fpdf2` with DejaVu and Noto/Ethiopic fallback fonts. Font cmap coverage is checked explicitly, so unsupported glyphs fail instead of being dropped. One clean PDF includes all prompt sections. Each character's PNG uses the identical filename stem.

`storage.py` uses private app-created Google Drive folders and only `drive.file` OAuth access. Uploads reconcile pre-reserved IDs, names, parents, MIME types, and checksums. `job-manifest.json` records stage/status, attempts, reports, usage, UTC timestamps, creator, snapshots, and file IDs without credentials. Job dates use Africa/Addis_Ababa. ZIPs contain durable original/output files and validation reports, excluding checkpoints/snapshots.

The job gate allows one active job per process. New submissions save as pending. After a restart a user resumes explicitly; there is no background worker or distributed locking. The CLI uses the same parser, services, authentication, snapshots, and resume behavior and must not run concurrently with a hosted instance against the same root.

Automated tests inject failures before and after snapshot/checkpoint/output uploads and manifest completion updates. They also exercise hashed login/sign-out, admin restrictions, shared history, duplicate/stale-edit protection, character deletion, PDF round trips, Ethiopic, and real SDK serialization with local transports. Live provider/Drive/hosting acceptance requires configured credentials; see README.md.
