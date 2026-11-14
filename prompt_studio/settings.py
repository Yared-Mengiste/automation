import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    anthropic_key: str = field(default="", repr=False)
    claude_model: str = "claude-sonnet-5-5"
    api_timeout: float = 240
    max_pdf_bytes: int = 10_000_000
    max_pages: int = 30
    max_characters: int = 40_000
    drive_client_id: str = field(default="", repr=False)
    drive_client_secret: str = field(default="", repr=False)
    drive_refresh_token: str = field(default="", repr=False)
    drive_root_id: str = ""

    @classmethod
    def from_mapping(cls, values):
        return cls(
            anthropic_key=values.get("ANTHROPIC_API_KEY", ""),
            claude_model=values.get("CLAUDE_MODEL", cls.claude_model),
            drive_client_id=values.get("GOOGLE_CLIENT_ID", ""),
            drive_client_secret=values.get("GOOGLE_CLIENT_SECRET", ""),
            drive_refresh_token=values.get("GOOGLE_REFRESH_TOKEN", ""),
            drive_root_id=values.get("DRIVE_ROOT_ID", ""),
        )

    @classmethod
    def from_env(cls):
        return cls.from_mapping(os.environ)
