"""Groq free plan: Whisper speech-to-text and an LLM with strict JSON output.

Free plan limits (Oct 2026, check console.groq.com): Whisper ~2,000 requests and ~8 h of audio per
day, 25 MB per file; LLMs ~1,000 requests / ~200k tokens per day per model. When a limit is hit Groq
answers HTTP 429; we raise RateLimited with how long to wait, and the caller falls back to local.
By default Groq does not keep request data; you can also turn on Zero Data Retention in its Data Controls.
"""
from __future__ import annotations

import io
import json
import wave

import httpx
import numpy as np

BASE = "https://api.groq.com/openai/v1"
STT_MODELS = ["whisper-large-v3", "whisper-large-v3-turbo"]
# each model has its own free daily allowance (checked 16-Oct: 8,000 tokens/minute, 1,000 requests/day each)
LLM_MODELS = ["openai/gpt-oss-120b", "qwen/qwen3.8-27b", "openai/gpt-oss-20b"]   # 16-Oct test: qwen wrote better points than 20b


class GroqError(RuntimeError):
    pass


class RateLimited(GroqError):
    """kind: 'day' (daily tokens/requests used up: try another model) or 'minute' (wait and retry).
    too_large: this request can never fit the per-minute limit (send less)."""

    def __init__(self, msg: str, retry_after: float, kind: str | None = None, too_large: bool = False):
        super().__init__(msg)
        self.retry_after = retry_after
        self.kind = kind or ("day" if retry_after > 90 else "minute")
        self.too_large = too_large


# Per-model answer size. Checked 16-Oct: qwen3.8-27b allows only 1,000 OUTPUT tokens per minute,
# so asking for more makes every request fail with "Request too large".
MODEL_MAX_OUT = {"qwen/qwen3.8-27b": 900}


def _wait_in_text(text: str) -> float | None:
    """Groq writes 'Please try again in 1m26.4s' / '7.5s' / '350ms' in the error message."""
    import re
    m = re.search(r"try again in\s+(?:(\d+)h)?(?:(\d+)m(?!s))?(?:([\d.]+)s)?(?:([\d.]+)ms)?", text)
    if not m or not any(m.groups()):
        return None
    h, mi, s, ms = m.groups()
    return int(h or 0) * 3600 + int(mi or 0) * 60 + float(s or 0) + float(ms or 0) / 1000


def _retry_after(r: httpx.Response) -> float:
    for h in ("retry-after", "x-ratelimit-reset-requests", "x-ratelimit-reset-tokens"):
        v = r.headers.get(h)
        if v:
            try:
                s = v.strip().lower()
                if s.endswith("ms"):
                    return float(s[:-2]) / 1000
                if s.endswith("s") and s[:-1].replace(".", "").isdigit():
                    return float(s[:-1])
                if "m" in s and s.endswith("s"):  # e.g. 7m12.5s
                    m, sec = s[:-1].split("m", 1)
                    return float(m) * 60 + float(sec or 0)
                return float(s)
            except ValueError:
                continue
    return 600.0


def _check(r: httpx.Response) -> dict:
    if r.status_code in (429, 413) and ("rate limit" in r.text.lower() or "too large" in r.text.lower()
                                         or r.status_code == 429):
        text = r.text
        daily = "per day" in text or "(TPD)" in text or "(RPD)" in text
        wait = _wait_in_text(text)
        raise RateLimited(f"Groq free limit reached: {text[:300]}",
                          wait if wait is not None else _retry_after(r),
                          kind="day" if daily else "minute", too_large="too large" in text.lower())
    if r.status_code == 401:
        raise GroqError("Groq API key is wrong or expired.")
    if r.status_code >= 400:
        raise GroqError(f"Groq error {r.status_code}: {r.text[:300]}")
    return r.json()


def wav_bytes(audio: np.ndarray, rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())
    return buf.getvalue()


class Groq:
    def __init__(self, api_key: str, client: httpx.Client | None = None, timeout: float = 120):
        self.client = client or httpx.Client(timeout=timeout)
        self.headers = {"Authorization": f"Bearer {api_key}"}

    def transcribe(self, audio: np.ndarray, model: str = "whisper-large-v3", language: str | None = "en",
                   prompt: str | None = None) -> tuple[list[tuple[float, float, str, float, float]], str | None]:
        """Returns ([(start, end, text, avg_logprob, no_speech_prob)], language)."""
        data = {"model": model, "response_format": "verbose_json", "temperature": "0"}
        if language:
            data["language"] = language
        if prompt:
            data["prompt"] = prompt[:800]  # Whisper reads at most ~224 tokens of prompt
        r = self.client.post(f"{BASE}/audio/transcriptions", headers=self.headers, data=data,
                             files={"file": ("speech.wav", wav_bytes(audio), "audio/wav")})
        body = _check(r)
        segs = [(float(s.get("start", 0)), float(s.get("end", 0)), s.get("text", ""),
                 float(s.get("avg_logprob", 0.0)), float(s.get("no_speech_prob", 0.0)))
                for s in body.get("segments") or []]
        if not segs and body.get("text"):
            segs = [(0.0, len(audio) / 16000, body["text"], 0.0, 0.0)]
        return segs, body.get("language")

    def chat_json(self, system: str, user: str, schema: dict, model: str = "openai/gpt-oss-120b",
                  name: str = "notes", max_tokens: int = 1800) -> tuple[dict, dict]:
        """Strict JSON-schema output. Returns (data, usage). Falls back to json_object mode once if
        the strict schema is rejected."""
        msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        extra = {"max_completion_tokens": min(max_tokens, MODEL_MAX_OUT.get(model, max_tokens))}
        if model.startswith("openai/gpt-oss"):
            extra["reasoning_effort"] = "low"   # thinking tokens count against the free per-minute limit
        payload = {"model": model, "messages": msgs, "temperature": 0.2, **extra,
                   "response_format": {"type": "json_schema",
                                       "json_schema": {"name": name, "schema": schema, "strict": True}}}
        r = self.client.post(f"{BASE}/chat/completions", headers=self.headers, json=payload)
        if r.status_code == 400 and "json" in r.text.lower():
            msgs[0] = {"role": "system", "content": system + "\n\nReply with JSON only, matching this JSON schema:\n"
                       + json.dumps(schema)}
            payload = {"model": model, "messages": msgs, "temperature": 0.2, **extra,
                       "response_format": {"type": "json_object"}}
            r = self.client.post(f"{BASE}/chat/completions", headers=self.headers, json=payload)
        body = _check(r)
        choice = body["choices"][0]
        if choice.get("finish_reason") == "length":
            raise GroqError("413 answer too large for max_completion_tokens; part too large")
        text = choice["message"]["content"]
        return json.loads(text), body.get("usage") or {}
