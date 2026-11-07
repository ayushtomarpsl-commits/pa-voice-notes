"""API keys (module named keys.py so it never shadows the stdlib `secrets`), encrypted for the current Windows user (DPAPI) in data/secrets.dpapi.

Names: 'groq', 'anthropic'. An environment variable (GROQ_API_KEY / ANTHROPIC_API_KEY) wins.
The old single-key file data/api_key.dpapi (Anthropic) is still read.
"""
from __future__ import annotations

import json
import os

from . import config

ENV = {"groq": "GROQ_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}


def _path():
    return config.DATA_DIR / "secrets.dpapi"


def _load() -> dict:
    from .winops import dpapi_decrypt
    data = {}
    if _path().exists():
        try:
            data = json.loads(dpapi_decrypt(_path().read_bytes()).decode("utf-8"))
        except (OSError, ValueError):
            data = {}
    if "anthropic" not in data and config.SECRET_PATH.exists():  # old file
        try:
            data["anthropic"] = dpapi_decrypt(config.SECRET_PATH.read_bytes()).decode("utf-8")
        except OSError:
            pass
    return data


def get(name: str) -> str | None:
    env = ENV.get(name)
    if env and os.environ.get(env):
        return os.environ[env]
    return _load().get(name) or None


def set(name: str, value: str | None) -> None:
    from .winops import dpapi_encrypt
    data = _load()
    if value and value.strip():
        data[name] = value.strip()
    else:
        data.pop(name, None)
        if name == "anthropic":
            config.SECRET_PATH.unlink(missing_ok=True)
    config.ensure_dirs()
    _path().write_bytes(dpapi_encrypt(json.dumps(data).encode("utf-8")))


def status() -> dict:
    return {n: bool(get(n)) for n in ENV}
