"""
env_loader — load local secrets from config/secrets.env into os.environ.

Called at import time by any scanner or runner that needs credentialed API
access. Only populates a key if it's not already set in the environment
(system env vars win over the file), so this plays nicely with CI / Windows
`setx` / Docker secrets if you ever layer those on top.

Usage:
    from env_loader import load_env
    load_env()  # idempotent — safe to call multiple times

The secrets file format is plain KEY=VALUE lines, no quoting or escaping.
Comments start with '#'. Missing file is a no-op (not an error) so scanners
without credentials still run cleanly and return status=auth_required.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

_REPO = Path(__file__).resolve().parent.parent
_DEFAULT_PATH = _REPO / "config" / "secrets.env"
_LOADED = False


def load_env(path: Optional[Path] = None) -> int:
    """Load KEY=VALUE pairs from secrets.env into os.environ."""
    global _LOADED
    if _LOADED and path is None:
        return 0

    target = Path(path) if path else _DEFAULT_PATH
    if not target.exists():
        if path is None:
            _LOADED = True
        return 0

    n_set = 0
    try:
        for raw in target.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            if not key or os.environ.get(key):
                continue
            os.environ[key] = value
            n_set += 1
    except Exception:
        pass

    if path is None:
        _LOADED = True
    return n_set


try:
    load_env()
except Exception:
    pass
