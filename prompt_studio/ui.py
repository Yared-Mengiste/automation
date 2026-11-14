"""Shared, native Streamlit presentation for the light studio theme."""

import streamlit as st

# Only static presentation enters HTML. Character, account and job content is
# rendered through native components below and in app.py.
STUDIO_CSS = """
<style>
.stApp:has(.st-key-studio) [data-testid="stMainBlockContainer"] {
    max-width: 1240px;
    padding-top: 2.5rem;
    padding-bottom: 3rem;
}
.st-key-studio, .st-key-studio-sidebar {
    --studio-border: #E4E1ED;
    --studio-muted: #655E75;
}
.st-key-studio h1 { font-size: 2.25rem; letter-spacing: -0.04em; }
.st-key-studio h2 { font-size: 1.35rem; letter-spacing: -0.02em; }
.st-key-studio h3 { font-size: 1.12rem; }
.st-key-studio h1, .st-key-studio h2, .st-key-studio h3,
.st-key-studio-sidebar h2 { overflow-wrap: anywhere; }
.st-key-studio [data-testid="stCaptionContainer"],
.st-key-studio-sidebar [data-testid="stCaptionContainer"] {
    color: var(--studio-muted);
    opacity: 1;
}
.st-key-studio div[class*="st-key-studio-character-"],
.st-key-studio div[class*="st-key-studio-job-"],
.st-key-studio .st-key-studio-upload,
.st-key-studio .st-key-studio-selection,
.st-key-studio .st-key-studio-preview,
.st-key-studio .st-key-studio-generation,
.st-key-studio .st-key-studio-accounts,
.st-key-studio .st-key-studio-configuration,
.st-key-studio .st-key-studio-empty {
    background: #FFFFFF;
    border-radius: 16px;
    box-shadow: 0 4px 18px rgb(36 32 51 / 3%);
}
.st-key-studio [data-testid="stForm"] {
    background: #FFFFFF;
    border-color: var(--studio-border);
    border-radius: 14px;
}
.st-key-studio [data-testid="stExpander"] details {
    border-color: var(--studio-border);
    border-radius: 12px;
    background: #FFFFFF;
}
.st-key-studio [data-testid="stExpander"] summary p { overflow-wrap: anywhere; }
.st-key-studio [data-testid="stFileUploaderDropzone"] {
    background: #F7F7FB;
    border: 1px dashed #C9BDE1;
    border-radius: 12px;
}
.st-key-studio [data-testid="stMetricLabel"] { color: var(--studio-muted); }
.st-key-studio [data-testid="stMetricValue"] { font-size: 1.8rem; }
.st-key-studio button, .st-key-studio a[data-testid="stBaseLinkButton"],
.st-key-studio-sidebar button { border-radius: 10px; min-height: 2.75rem; }
.st-key-studio :is(button, a, input, textarea, [tabindex]):focus-visible,
.st-key-studio-sidebar :is(button, a, input, [tabindex]):focus-visible {
    outline: 3px solid #7C3AED;
    outline-offset: 3px;
}
.st-key-studio div[class*="st-key-studio-character-"] [data-testid="stImage"] {
    width: 100%;
}
.st-key-studio div[class*="st-key-studio-character-"] [data-testid="stImage"] img {
    width: 100%;
    height: 240px;
    object-fit: contain;
    border-radius: 10px;
    background: #F7F7FB;
}
.st-key-studio:has(.st-key-studio-login) {
    min-height: calc(100dvh - 7rem);
    justify-content: center;
}
.st-key-studio .st-key-studio-login {
    max-width: 460px;
    margin: 0 auto;
    padding: 2.25rem;
    background: #FFFFFF;
    border: 1px solid var(--studio-border);
    border-radius: 20px;
    box-shadow: 0 12px 40px rgb(36 32 51 / 6%);
}
.st-key-studio-login h1 { font-size: 2rem; }
.st-key-studio-login [data-testid="stForm"] { border: 0; padding: 0; }
.stApp:has(.st-key-studio) [data-testid="stSidebar"] {
    background: #FFFFFF;
    border-right: 1px solid #E4E1ED;
}
.st-key-studio-sidebar [data-testid="stRadio"] label {
    padding: 0.4rem 0;
    overflow-wrap: anywhere;
}
.st-key-studio-sidebar [data-testid="stRadio"] label:has(input:checked) {
    color: #6D28D9;
    font-weight: 600;
}
.st-key-studio-sidebar [data-testid="stText"] { overflow-wrap: anywhere; }
@media (max-width: 760px) {
    .stApp:has(.st-key-studio) [data-testid="stMainBlockContainer"] {
        padding: 4.5rem 1rem 2rem;
    }
    .st-key-studio h1 { font-size: 1.8rem; }
    .st-key-studio [data-testid="stHorizontalBlock"]:has(> [data-testid="stColumn"]) {
        flex-direction: column;
        gap: 1rem;
    }
    .st-key-studio [data-testid="stColumn"] {
        width: 100% !important;
        flex: 1 1 100% !important;
        min-width: 0;
    }
    .st-key-studio .st-key-studio-login { padding: 1.5rem; margin-top: 0; }
    .st-key-studio div[class*="st-key-studio-character-"] [data-testid="stImage"] img {
        height: 260px;
    }
}
</style>
"""


def apply_styles():
    st.html(STUDIO_CSS)


def page_header(title, description):
    st.title(title)
    st.caption(description)


def step_header(number, title, description):
    st.subheader(f"{number:02d} · {title}")
    st.caption(description)


def status_badge(status):
    colors = {
        "active": "violet",
        "archived": "gray",
        "complete": "green",
        "failed": "red",
        "partial": "orange",
        "pending": "gray",
        "preparing": "blue",
        "running": "violet",
    }
    st.badge(status.capitalize(), color=colors.get(status, "gray"))


def empty_state(title, description):
    with st.container(border=True, key="studio-empty"):
        st.subheader(title)
        st.info(description)


def character_details(character):
    for label, name in [
        ("Pronouns", "pronouns"),
        ("Voice and accent", "voice"),
        ("Personality", "personality"),
        ("Wardrobe", "wardrobe"),
        ("Character negatives", "negatives"),
        ("Consistency instructions", "consistency"),
    ]:
        st.caption(label)
        st.text(getattr(character, name))
    st.caption("Exact image mapping")
    st.code(character.mapping, language=None, wrap_lines=True)


def character_card(principal, library, character, *, selected=False):
    """Render the common image and identity within the caller's card."""
    st.image(
        library.image(principal, character),
        width="stretch",
        alt=f"Reference image for {character.display_name}",
    )
    st.subheader(character.display_name)
    with st.container(horizontal=True):
        status_badge(character.status)
        st.caption(f"Version {character.version}")
    if selected:
        st.caption("Voice and personality")
        st.text(character.voice)
        st.text(character.personality)
    with st.expander("Character details"):
        character_details(character)
        if not selected:
            st.caption(f"Filename slug: {character.slug} · ID: {character.id}")


def job_counts(manifest):
    entries = manifest["characters"]
    complete = sum(entry["status"] == "complete" for entry in entries)
    failed = sum(entry["status"] == "failed" for entry in entries)
    return complete, failed, len(entries)


def configuration_indicators(values):
    """Render presence only; credential values never enter a UI component."""
    for label, names in [
        ("Claude API", ["ANTHROPIC_API_KEY"]),
        ("Google OAuth client ID", ["GOOGLE_CLIENT_ID"]),
        ("Google OAuth client secret", ["GOOGLE_CLIENT_SECRET"]),
        ("Google Drive authorization", ["GOOGLE_REFRESH_TOKEN"]),
        ("Private Drive folder", ["DRIVE_ROOT_ID"]),
    ]:
        configured = all(bool(values.get(name)) for name in names)
        with st.container(horizontal=True):
            st.badge(
                "Configured" if configured else "Missing", color="green" if configured else "orange"
            )
            st.write(label)
