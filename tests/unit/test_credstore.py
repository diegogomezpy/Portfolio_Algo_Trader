"""Unit tests for engine.credstore — the encrypted per-account credential store (ADR-001 Phase C).

In-memory sqlite + an injected Fernet key. Guards the round-trip, that ciphertext never leaks the
secret, that list/metadata surfaces carry NO secrets, upsert-by-slug, removal, enable/disable, and
that the wrong key can't decrypt.
"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, select

from engine import credstore, db

_KEY = Fernet.generate_key()
_KEY2 = Fernet.generate_key()


def _engine():
    eng = create_engine("sqlite://")
    db.create_all(eng)
    return eng


def test_add_then_get_round_trips():
    eng = _engine()
    info = credstore.add_account(eng, slug="trend", api_key="PKLIVE1234ABCD",
                                 api_secret="s3cr3t-value", label="Trend sleeve",
                                 capital=250_000, leverage=1.0, key=_KEY)
    assert info["slug"] == "trend" and info["key_fingerprint"] == "PKL…ABCD"
    assert "api_secret" not in info and "api_key" not in info      # returns no secret material
    creds = credstore.get_credentials(eng, "trend", key=_KEY)
    assert creds == {"api_key": "PKLIVE1234ABCD", "api_secret": "s3cr3t-value",
                     "base_url": "https://paper-api.alpaca.markets"}


def test_ciphertext_does_not_contain_the_secret():
    eng = _engine()
    credstore.add_account(eng, slug="a", api_key="PKAAAA1111", api_secret="TOPSECRET", key=_KEY)
    (row,) = [dict(r) for r in
              eng.connect().execute(select(db.account_credentials)).mappings().all()]
    blob = bytes(row["ciphertext"])
    assert b"TOPSECRET" not in blob and b"PKAAAA1111" not in blob   # encrypted at rest
    assert row["key_fingerprint"] == "PKA…1111"                    # only the masked id is clear


def test_list_accounts_carries_no_secrets():
    eng = _engine()
    credstore.add_account(eng, slug="a", api_key="PKAAAA1111", api_secret="x", key=_KEY,
                          capital=1_000_000, leverage=2.0)
    (acct,) = credstore.list_accounts(eng)
    assert acct["slug"] == "a" and acct["enabled"] is True
    assert acct["capital"] == 1_000_000 and acct["leverage"] == 2.0
    assert set(acct) == {"slug", "label", "base_url", "key_fingerprint", "capital",
                         "leverage", "enabled", "updated_at"}      # no api_key / api_secret keys


def test_upsert_by_slug_updates_not_duplicates():
    eng = _engine()
    credstore.add_account(eng, slug="a", api_key="PKOLD00000", api_secret="old", key=_KEY)
    credstore.add_account(eng, slug="a", api_key="PKNEW11111", api_secret="new", key=_KEY)
    accts = credstore.list_accounts(eng)
    assert len(accts) == 1 and accts[0]["key_fingerprint"] == "PKN…1111"
    assert credstore.get_credentials(eng, "a", key=_KEY)["api_secret"] == "new"


def test_remove_and_missing():
    eng = _engine()
    credstore.add_account(eng, slug="a", api_key="PKAAAA1111", api_secret="x", key=_KEY)
    assert credstore.remove_account(eng, "a") is True
    assert credstore.remove_account(eng, "a") is False            # already gone
    with pytest.raises(KeyError):
        credstore.get_credentials(eng, "a", key=_KEY)


def test_set_enabled():
    eng = _engine()
    credstore.add_account(eng, slug="a", api_key="PKAAAA1111", api_secret="x", key=_KEY)
    credstore.set_enabled(eng, "a", False)
    assert credstore.list_accounts(eng)[0]["enabled"] is False


def test_wrong_key_cannot_decrypt():
    from cryptography.fernet import InvalidToken
    eng = _engine()
    credstore.add_account(eng, slug="a", api_key="PKAAAA1111", api_secret="x", key=_KEY)
    with pytest.raises(InvalidToken):
        credstore.get_credentials(eng, "a", key=_KEY2)            # a leaked DB w/o the key is useless


def test_add_validates_inputs():
    eng = _engine()
    with pytest.raises(ValueError):
        credstore.add_account(eng, slug="", api_key="k", api_secret="s", key=_KEY)
    with pytest.raises(ValueError):
        credstore.add_account(eng, slug="a", api_key="", api_secret="s", key=_KEY)


def test_key_from_env(monkeypatch):
    # With no injected key, the store reads SEPI_CRED_KEK from the env (no file touched).
    monkeypatch.setenv("SEPI_CRED_KEK", _KEY.decode())
    eng = _engine()
    credstore.add_account(eng, slug="a", api_key="PKAAAA1111", api_secret="env-secret")
    assert credstore.get_credentials(eng, "a")["api_secret"] == "env-secret"
    assert credstore.get_credentials(eng, "a", key=_KEY)["api_secret"] == "env-secret"  # same key


def test_add_strips_whitespace_from_pasted_creds():
    # Pasting from another tab commonly drags a trailing newline/space — Alpaca then 401s. The
    # store must strip so a working paste isn't rejected as "unauthorized".
    eng = _engine()
    credstore.add_account(eng, slug="a", api_key="  PKAAAA1111\n", api_secret="\tsecret-val ",
                          key=_KEY)
    creds = credstore.get_credentials(eng, "a", key=_KEY)
    assert creds["api_key"] == "PKAAAA1111" and creds["api_secret"] == "secret-val"
