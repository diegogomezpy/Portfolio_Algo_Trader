"""Every settings.yaml leaf key must be referenced somewhere in the code (audit section D).

The 2026-07 audit found SEVEN settings that nothing read — including
``close_before_earnings: true``, which claimed to be a risk control while the behavior it
described ran unconditionally. A config key the code never reads is worse than dead weight:
it documents behavior that doesn't exist and silently ignores operator changes.

Heuristic: the key's name must appear as a word in engine/, scripts/ or dashboard/ source.
That's deliberately loose (it can't prove the *value* is honored) but it catches the whole
"key exists, nothing reads it" class — which is exactly what rotted pre-audit.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SCAN_DIRS = ("engine", "scripts", "dashboard")

# Structural keys whose CHILDREN are the real settings (the parent name may legitimately
# only appear in YAML), plus list-valued entries referenced by their parent.
_STRUCTURAL = {"weights"}


def _leaf_keys(node, out: set[str]) -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(v, dict):
                if k not in _STRUCTURAL:
                    out.add(k) if not isinstance(v, dict) else None
                _leaf_keys(v, out)
            else:
                out.add(k)


def test_every_settings_leaf_key_is_referenced_in_code():
    data = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text())
    keys: set[str] = set()
    _leaf_keys(data, keys)
    corpus = "\n".join(p.read_text() for d in SCAN_DIRS for p in (ROOT / d).rglob("*.py")
                       if not p.name.startswith("."))   # skip macOS ._AppleDouble shadows
    dead = sorted(k for k in keys if not re.search(rf"\b{re.escape(k)}\b", corpus))
    assert not dead, (
        f"settings.yaml keys never referenced in engine/scripts/dashboard: {dead} — "
        f"either wire them or delete them (see the 2026-07 audit)")
