"""Paths and settings. Settings live in data/config.json; missing keys fall back to DEFAULTS."""
from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("PA_DATA_DIR", ROOT / "data"))
DB_PATH = DATA_DIR / "pa.sqlite"
CONFIG_PATH = DATA_DIR / "config.json"
QUEUE_DIR = DATA_DIR / "queue"          # speech waiting to be transcribed (deleted after)
AUDIO_DIR = DATA_DIR / "audio"          # only used when keep_audio_days > 0
EXPORT_DIR = ROOT / "exports"           # daily hand-off files for Claude Code
LOG_DIR = DATA_DIR / "logs"
SECRET_PATH = DATA_DIR / "api_key.dpapi"

DEFAULTS: dict = {
    "port": 8765,
    # --- listening ---
    "mic_device": "",                   # "" = Windows default microphone; else part of the device name
    "capture_calls": True,              # also record PC sound (the other side of Teams/Meet/Zoom)
    "work_hours": {"enabled": True, "start": "09:00", "end": "20:00", "days": [0, 1, 2, 3, 4, 5]},  # Mon=0
    "lunch": {"enabled": False, "start": "13:30", "end": "14:15"},
    "vad_threshold": 0.5,
    "min_speech_ms": 600,               # shorter speech is ignored (coughs, clicks)
    "max_utterance_s": 28,              # cut long speech so Whisper gets <30 s pieces
    "silence_end_ms": 900,              # this much silence ends an utterance
    # --- speech to text ---
    "stt_engine": "groq",               # groq = free Groq Whisper large-v3 (cloud) with local fallback; local = laptop only
    "groq_stt_model": "whisper-large-v3",
    "groq_language": "hi",              # tested 16-Oct on real Hindi clips: "hi" 6/6 correct and English stays English;
                                        # "en" translated 3/6; "" (auto) gave Urdu script 2/6.
    "whisper_model": "small",           # local model: tiny | base | small | medium | large-v3-turbo | hinglish-swift | hinglish-prime
    "language": "hi",                   # laptop model. "hi" = Hindi in Devanagari, English stays English (tested best).
                                        # "en" TRANSLATES Hindi into English (meaning gets lost). "" = auto (may give Urdu script).
    "cpu_threads": 8,
    "beam_size": 1,
    "vocabulary": "CRM, ERP, API, MCP, Angular, React, .NET, SQL, QA, PROD, P0, P1, deploy, "
                  "stored procedure",   # add your product names, people and terms in Settings
    "battery_mode": "defer",            # defer = on battery, only queue speech; transcribe on charger. "always" = transcribe anyway
    "low_priority": True,               # run below normal CPU priority so VS Code / builds stay fast
    "keep_audio_days": 0,               # 0 = never keep audio after transcription
    # --- who is speaking ---
    "voice_id": True,                   # recognise voices and name speakers (fingerprints stay on this laptop)
    "speaker_threshold": 0.50,          # higher = stricter (more new names), lower = more merging
    "speaker_min_new_seconds": 2.5,     # a new person is only created from this much clear speech
    "my_name": "Me",                    # the voice that dominates your microphone gets this name (set yours in Settings)
    "people": {},                       # teammates the Hinglish tagger should spot: {"Name": ["alias", "नाम"], ...}
    # --- notes ---
    "conversation_gap_min": 4,          # silence longer than this starts a new conversation
    "max_conversation_min": 20,         # a non-stop talk is cut into parts of this length (notes come regularly)
    "groq_chunk_tokens": 1500,          # transcript size per Groq request (free plan: ~8,000 tokens per minute)
    "auto_process": True,               # make notes automatically when a conversation ends (needs a Groq or Claude key)
    "llm_engine": "groq",               # groq = free Groq LLM | claude = Claude API (paid) | claude-code = only via /pa-process
    "groq_llm_model": "openai/gpt-oss-120b",
    "office_only": True,                # keep notes/to-dos only for office talk; personal and small talk are skipped
    "delete_non_work_text": False,      # also delete the transcript text of skipped (non-office) conversations
    "min_words_for_notes": 25,          # shorter conversations are marked small talk without calling any AI
    "claude_model": "claude-opus-5-5",
    "claude_effort": "medium",
    "projects": ["Product A", "Product B", "Internal tools", "Personal", "Other"],
    # --- privacy ---
    "personal_words": ["side business", "family", "doctor", "salary", "shopping"],  # lines with these = Personal
    "hotkey": "ctrl+alt+p",
    "exclude_personal_from_handoff": True,
}

_lock = threading.Lock()


def ensure_dirs() -> None:
    for d in (DATA_DIR, QUEUE_DIR, AUDIO_DIR, EXPORT_DIR, LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load() -> dict:
    ensure_dirs()
    with _lock:
        if CONFIG_PATH.exists():
            try:
                saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                saved = {}
        else:
            saved = {}
        return _merge(DEFAULTS, saved)


def save(changes: dict) -> dict:
    """Merge `changes` into the saved config. Unknown keys are ignored."""
    ensure_dirs()
    clean = {k: v for k, v in changes.items() if k in DEFAULTS}
    with _lock:
        saved = {}
        if CONFIG_PATH.exists():
            try:
                saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                saved = {}
        saved = _merge(saved, clean)
        tmp = CONFIG_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(saved, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, CONFIG_PATH)
    return load()
