"""Secrets-backed authentication; no plaintext password storage or bypass."""

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass

from .errors import GenerationError


@dataclass(frozen=True)
class Principal:
    username: str
    role: str
    credential_tag: str


def hash_password(password: str) -> str:
    if len(password) < 12 or len(password) > 1024:
        raise ValueError("Use a password between 12 and 1024 characters.")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)
    return (
        "scrypt$16384$8$1$"
        + base64.b64encode(salt).decode()
        + "$"
        + base64.b64encode(digest).decode()
    )


def decode_hash(encoded: str) -> tuple[bytes, bytes]:
    try:
        algorithm, n, r, p, salt, digest = encoded.split("$")
        if (algorithm, n, r, p) != ("scrypt", "16384", "8", "1"):
            raise ValueError
        salt, digest = (
            base64.b64decode(salt, validate=True),
            base64.b64decode(digest, validate=True),
        )
        if len(salt) != 16 or len(digest) != 32:
            raise ValueError
        return salt, digest
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(
            "Invalid scrypt password hash; regenerate it with the local helper."
        ) from exc


class Accounts:
    def __init__(self, config):
        try:
            self.users = json.loads(config) if isinstance(config, str) else dict(config)
            if len(self.users) != 2:
                raise ValueError
            roles = []
            for name, account in self.users.items():
                if not isinstance(name, str) or not name.strip() or len(name) > 100:
                    raise ValueError
                if set(account) != {"password_hash", "role", "enabled"}:
                    raise ValueError
                decode_hash(account["password_hash"])
                if account["role"] not in {"admin", "creator"} or not isinstance(
                    account["enabled"], bool
                ):
                    raise ValueError
                roles.append(account["role"])
            if sorted(roles) != ["admin", "creator"]:
                raise ValueError
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise GenerationError(
                "Configure exactly one admin and one creator with valid scrypt hashes, roles, and enabled flags in APP_USERS_JSON."
            ) from exc
        # Unknown usernames use a real hash too, avoiding an inexpensive timing shortcut.
        self.dummy_hash = next(iter(self.users.values()))["password_hash"]

    def _tag(self, username):
        account = self.users[username]
        return hashlib.sha256(json.dumps(account, sort_keys=True).encode()).hexdigest()

    def authenticate(self, username: str, password: str) -> Principal | None:
        account = self.users.get(username)
        salt, expected = decode_hash(account["password_hash"] if account else self.dummy_hash)
        if len(password) > 1024:
            return None
        actual = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)
        valid = hmac.compare_digest(actual, expected)
        if valid and account and account["enabled"]:
            return Principal(username, account["role"], self._tag(username))
        return None

    def require(self, principal: Principal, admin=False) -> None:
        account = self.users.get(getattr(principal, "username", ""))
        if (
            not account
            or not account["enabled"]
            or principal.credential_tag != self._tag(principal.username)
            or principal.role != account["role"]
        ):
            raise GenerationError("Your session is no longer valid. Sign in again.")
        if admin and account["role"] != "admin":
            raise GenerationError("Only the admin can change the character library.")

    def status(self, principal):
        self.require(principal, admin=True)
        return [
            {
                "username": name,
                "role": value["role"],
                "enabled": value["enabled"],
                "password_configured": True,
            }
            for name, value in self.users.items()
        ]


def sign_out(session):
    session.clear()
