"""Encrypted per-account credential store — dashboard-managed broker keys (ADR-001 / D37 Phase C).

Diego's requirement: add a trading account entirely from the dashboard (type the key + secret,
click save, use it) — **no VM, no gcloud, ever**. So the credentials live encrypted in Postgres
rather than in Secret Manager (which would need a one-time IAM grant). Chosen 2026-07-08; the
security is on par with the existing ``.env.paper`` (the encryption key sits on the box the way
today's creds do) and strictly better against a leaked backup (ciphertext only). Migrate to
Secret Manager before go-live (Phase 7).

**The boundary:** the assistant never handles raw key values. The operator types them into the
dashboard; they flow browser → server → :func:`add_account` → Fernet ciphertext. Nothing here
logs, echoes, or returns a secret — reads that surface to the UI carry only a **masked
fingerprint**. :func:`get_credentials` (decrypt) is used solely by the engine to build a client.

**Encryption.** Fernet (AES-128-CBC + HMAC-SHA256, authenticated). The key comes from, in order:
``SEPI_CRED_KEK`` (env, a urlsafe-base64 Fernet key), then ``SEPI_CRED_KEK_FILE`` (path), else a
file auto-generated 0600 next to the app on first use. The key is **never** written to the DB, so
it is absent from ``pg_dump`` → a stolen backup is undecryptable. ``key=`` is injectable for tests.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from engine.logger import get_logger

log = get_logger(__name__)

# App-dir file (git-ignored), anchored to the repo root off this module — NOT cwd-relative, so the
# dashboard and eod processes resolve the SAME key regardless of their working directory. Auto-
# created 0600 on first use.
_DEFAULT_KEK_FILE = str(Path(__file__).resolve().parent.parent / ".cred_kek")


# ---------------------------------------------------------------------------- #
# Key management — never in the DB, so a leaked backup can't be decrypted.
# ---------------------------------------------------------------------------- #
def _load_or_create_key() -> bytes:
    """The Fernet key from env, else a file, else freshly generated + persisted 0600."""
    env_key = os.environ.get("SEPI_CRED_KEK", "").strip()
    if env_key:
        return env_key.encode()
    path = Path(os.environ.get("SEPI_CRED_KEK_FILE", "").strip() or _DEFAULT_KEK_FILE)
    if path.exists():
        return path.read_bytes().strip()
    from cryptography.fernet import Fernet
    key = Fernet.generate_key()
    path.write_bytes(key)
    try:
        path.chmod(0o600)
    except OSError:                       # best-effort on odd filesystems
        pass
    log.warning("generated a new credential encryption key", extra={"path": str(path)})
    return key


def _fernet(key: Optional[bytes] = None):
    from cryptography.fernet import Fernet
    return Fernet(key or _load_or_create_key())


def _fingerprint(api_key: str) -> str:
    """A safe-to-display id for an api key: first 3 + last 4 (the api key is the public-ish half;
    the SECRET is never fingerprinted or shown)."""
    k = str(api_key)
    return f"{k[:3]}…{k[-4:]}" if len(k) > 8 else "…"


# ---------------------------------------------------------------------------- #
# Store operations
# ---------------------------------------------------------------------------- #
def add_account(db_engine, *, slug: str, api_key: str, api_secret: str,
                label: str | None = None, base_url: str = "https://paper-api.alpaca.markets",
                capital: float | None = None, leverage: float | None = None,
                key: Optional[bytes] = None) -> dict:
    """Encrypt + store one account's credentials; upsert by ``slug``. Returns non-secret info
    (masked fingerprint) — never the key/secret. Raises ``ValueError`` on empty inputs.

    The plaintext key/secret exist only transiently here to encrypt; they are never logged and
    never written except as ciphertext.
    """
    slug = str(slug or "").strip().lower()
    if not slug:
        raise ValueError("slug is required")
    if not (api_key and api_secret):
        raise ValueError("api_key and api_secret are required")
    token = _fernet(key).encrypt(json.dumps({"api_key": api_key, "api_secret": api_secret}).encode())
    fp = _fingerprint(api_key)
    now = datetime.now(timezone.utc)
    from sqlalchemy import insert, update
    from engine.db import account_credentials as t
    with db_engine.begin() as conn:
        exists = conn.execute(t.select().where(t.c.slug == slug)).first()
        vals = dict(label=label or slug, base_url=base_url, ciphertext=token,
                    key_fingerprint=fp, capital=capital, leverage=leverage, updated_at=now)
        if exists:
            conn.execute(update(t).where(t.c.slug == slug).values(**vals))
        else:
            conn.execute(insert(t).values(slug=slug, enabled=True, **vals))
    log.warning("account credential stored", extra={"slug": slug, "fingerprint": fp})  # no secret
    return {"slug": slug, "label": vals["label"], "base_url": base_url,
            "key_fingerprint": fp, "enabled": True}


def get_credentials(db_engine, slug: str, *, key: Optional[bytes] = None) -> dict:
    """Decrypt one account's ``{api_key, api_secret, base_url}`` — engine-only (client build).
    Raises ``KeyError`` if the slug is unknown."""
    from engine.db import account_credentials as t
    with db_engine.connect() as conn:
        row = conn.execute(
            t.select().where(t.c.slug == str(slug).strip().lower())).mappings().first()
    if row is None:
        raise KeyError(f"no stored credentials for account {slug!r}")
    data = json.loads(_fernet(key).decrypt(bytes(row["ciphertext"])).decode())
    return {"api_key": data["api_key"], "api_secret": data["api_secret"],
            "base_url": row["base_url"]}


def list_accounts(db_engine) -> list[dict]:
    """All accounts as **non-secret** metadata for the dashboard — no key/secret, ever."""
    from engine.db import account_credentials as t
    with db_engine.connect() as conn:
        rows = conn.execute(t.select().order_by(t.c.slug)).mappings().all()
    return [{"slug": r["slug"], "label": r["label"], "base_url": r["base_url"],
             "key_fingerprint": r["key_fingerprint"], "capital": r["capital"],
             "leverage": r["leverage"], "enabled": bool(r["enabled"]),
             "updated_at": str(r["updated_at"]) if r["updated_at"] else None}
            for r in rows]


def remove_account(db_engine, slug: str) -> bool:
    """Delete an account's stored credentials. Returns True if a row was removed."""
    from sqlalchemy import delete
    from engine.db import account_credentials as t
    with db_engine.begin() as conn:
        res = conn.execute(delete(t).where(t.c.slug == str(slug).strip().lower()))
    removed = bool(res.rowcount)
    if removed:
        log.warning("account credential removed", extra={"slug": slug})
    return removed


def set_enabled(db_engine, slug: str, enabled: bool) -> None:
    """Enable/disable an account without deleting its stored credentials."""
    from sqlalchemy import update
    from engine.db import account_credentials as t
    with db_engine.begin() as conn:
        conn.execute(update(t).where(t.c.slug == str(slug).strip().lower())
                     .values(enabled=bool(enabled), updated_at=datetime.now(timezone.utc)))
