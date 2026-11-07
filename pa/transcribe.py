"""Speech to text on this laptop with faster-whisper.

Utterances from the VAD are first written to data/queue/*.wav, so nothing is lost if the app
crashes or the laptop sleeps. The worker thread turns each file into text, saves it, then deletes
the file. On battery (battery_mode='defer') the queue only grows and is worked off on the charger.
"""
from __future__ import annotations

import difflib
import logging
import re
import threading
import time
import wave
from datetime import datetime
from pathlib import Path

import numpy as np

from . import config, db, heuristics
from .vad import RATE, Utterance
from .winops import power_status

log = logging.getLogger("pa.stt")

# Whisper makes these up during silence or music.
JUNK = re.compile(
    r"^(thank(s| you)( so much)?( for watching| for listening)?[.!]?|subtitles? by.*|"
    r"(please )?(like and )?subscribe.*|you|bye[.!]?|\.+|music|\[.*\]|\(.*\))$", re.I)


def _stamp(t: float) -> str:
    return datetime.fromtimestamp(t).strftime("%Y-%m-%dT%H:%M:%S")


def save_utterance(u: Utterance, folder: Path | None = None) -> Path:
    folder = folder or config.QUEUE_DIR
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{u.start:.2f}_{u.end:.2f}_{u.source}.wav"
    pcm = (np.clip(u.audio, -1, 1) * 32767).astype("<i2").tobytes()
    tmp = path.with_suffix(".part")
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm)
    tmp.replace(path)
    return path


def load_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        if w.getframerate() != RATE or w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise ValueError(f"{path.name}: need 16 kHz mono 16-bit")
        return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768.0


def parse_name(path: Path) -> tuple[float, float, str]:
    start, end, source = path.stem.split("_", 2)
    return float(start), float(end), source


def clean_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    # collapse a phrase repeated 3+ times ("ok ok ok ok") - a classic Whisper loop
    text = re.sub(r"\b(.{2,40}?)(?:[\s,.]+\1\b){2,}", r"\1", text, flags=re.I)
    return "" if JUNK.match(text) else text


# Friendly names for local models -> what faster-whisper loads (a size name, HF repo, or folder)
LOCAL_MODELS = {
    "hinglish-prime": "Hub84/faster-whisper-hinglish-prime",       # Oriserve Hinglish, large size, Apache 2.0
    "hinglish-swift": str(config.ROOT / "models" / "hinglish-swift-ct2"),  # Oriserve Hinglish, base size
}


def local_model_path(name: str) -> str:
    return LOCAL_MODELS.get(name, name)


class LocalWhisper:
    """faster-whisper on this laptop's CPU."""

    def __init__(self, name: str, threads: int):
        import os
        os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
        from faster_whisper import WhisperModel
        self.name = name
        path = local_model_path(name)
        log.info("loading local whisper model %s ...", name)
        try:  # no network call when the model is already on disk
            self.model = WhisperModel(path, device="cpu", compute_type="int8", cpu_threads=threads,
                                      local_files_only=True)
        except Exception:
            log.info("downloading whisper model %s (one time) ...", name)
            self.model = WhisperModel(path, device="cpu", compute_type="int8", cpu_threads=threads)

    def segments(self, audio, language, beam, prompt):
        segs, info = self.model.transcribe(audio, language=language, beam_size=beam, vad_filter=False,
                                           condition_on_previous_text=False, initial_prompt=prompt)
        return [(s.start, s.end, s.text, s.avg_logprob, s.no_speech_prob) for s in segs], getattr(info, "language", None)


class Engine:
    """Speech-to-text with a fallback chain: Groq (free cloud, if chosen and a key is set) ->
    local model. When Groq says the free limit is reached, we use the local model until the
    wait time is over. Separate from the thread so tests can call it directly."""

    def __init__(self, cfg: dict, groq_client=None):
        self.cfg = cfg
        self._local: dict[str, LocalWhisper] = {}
        self._groq = groq_client
        self.groq_paused_until = 0.0
        self.last_engine = None      # which engine did the last piece, for the status bar
        self.last_error: str | None = None

    @property
    def model(self):  # kept for older callers
        return self.local(self.cfg["whisper_model"]).model

    def local(self, name: str) -> LocalWhisper:
        if name not in self._local:
            self._local[name] = LocalWhisper(name, int(self.cfg["cpu_threads"]))
        return self._local[name]

    def groq(self):
        if self._groq is None:
            from . import keys
            from .groq_api import Groq
            key = keys.get("groq")
            if not key:
                return None
            self._groq = Groq(key)
        return self._groq

    def use_groq(self) -> bool:
        return self.cfg.get("stt_engine") == "groq" and time.time() >= self.groq_paused_until

    def cloud_ready(self) -> bool:
        """True when the next piece would go to Groq (no laptop CPU needed)."""
        if not self.use_groq():
            return False
        try:
            return self.groq() is not None
        except Exception:
            return False

    def transcribe_segments(self, audio: np.ndarray, engine: str | None = None) -> tuple[list[tuple[float, float, str, float]], str | None]:
        """Returns ([(start_s, end_s, text, avg_logprob)], language). Times are inside `audio`.
        engine: None = follow settings; 'groq:<model>' or 'local:<model>' for the test bench."""
        lang = self.cfg["language"] or None
        prompt = self.cfg.get("vocabulary") or None
        raw = None
        if engine is None and self.use_groq() or (engine or "").startswith("groq:"):
            from .groq_api import GroqError, RateLimited
            g = self.groq()
            model = engine.split(":", 1)[1] if engine else self.cfg.get("groq_stt_model", "whisper-large-v3")
            if g is not None:
                try:
                    raw = g.transcribe(audio, model, self.cfg.get("groq_language") or None, prompt)
                    self.last_engine, self.last_error = f"groq:{model}", None
                except RateLimited as e:
                    self.groq_paused_until = time.time() + e.retry_after
                    self.last_error = f"Groq free limit reached; local model for {int(e.retry_after // 60)} min"
                    log.warning(self.last_error)
                    db.log_event("groq_limit", self.last_error)
                    if engine:
                        raise
                except (GroqError, OSError, Exception) as e:  # network down, bad key ...
                    self.groq_paused_until = time.time() + 300
                    self.last_error = f"Groq failed ({e}); local model for 5 min"
                    log.warning(self.last_error)
                    if engine:
                        raise
            elif engine:
                raise RuntimeError("No Groq API key saved")
        if raw is None:
            name = engine.split(":", 1)[1] if engine else self.cfg["whisper_model"]
            raw = self.local(name).segments(audio, lang, int(self.cfg["beam_size"]), prompt)
            self.last_engine = f"local:{name}"
        segs, language = raw
        out = []
        for s0, s1, text, logp, nsp in segs:
            if nsp > 0.6 and logp < -1.0:
                continue
            t = clean_text(text)
            if t:
                out.append((s0, s1, t, logp))
        return out, language

    def transcribe(self, audio: np.ndarray) -> tuple[str, str | None, float | None]:
        """Returns (text, language, average log-probability) for one piece."""
        segs, lang = self.transcribe_segments(audio)
        logps = [s[3] for s in segs]
        return clean_text(" ".join(s[2] for s in segs)), lang, (sum(logps) / len(logps) if logps else None)


def is_echo(source: str, text: str, start: str, end: str) -> bool:
    """Without headphones the mic also hears the call audio. If the mic text matches what the
    'call' source said at the same moment, it is an echo and we drop the mic copy."""
    if source != "me":
        return False
    for other in db.segments_between("call", start, end):
        if difflib.SequenceMatcher(None, text.lower(), other["text"].lower()).ratio() > 0.6:
            return True
    return False


class Transcriber(threading.Thread):
    def __init__(self, cfg: dict, on_segment=None):
        super().__init__(daemon=True, name="transcriber")
        self.cfg = cfg
        self.engine = Engine(cfg)
        self.on_segment = on_segment
        self.wake = threading.Event()
        self._stop = threading.Event()
        self.state = "idle"     # idle | working | waiting_for_charger | error
        self.done_count = 0
        self._voice = None      # VoiceModel, False when unavailable
        self._last_speaker = None

    def queue_size(self) -> int:
        return len(list(config.QUEUE_DIR.glob("*.wav")))

    def submit(self, u: Utterance) -> None:
        save_utterance(u)
        self.wake.set()

    def stop(self) -> None:
        self._stop.set()
        self.wake.set()

    def run(self) -> None:
        while not self._stop.is_set():
            files = sorted(config.QUEUE_DIR.glob("*.wav"))
            if not files:
                self.state = "idle"
                self.wake.wait(5)
                self.wake.clear()
                continue
            if (self.cfg.get("battery_mode") == "defer" and not power_status()["on_ac"]
                    and not self.engine.cloud_ready()):   # Groq does the work in the cloud: fine on battery
                self.state = "waiting_for_charger"
                self.wake.wait(30)
                self.wake.clear()
                continue
            self.state = "working"
            group = pick_group(files)
            try:
                self.process_group(group)
            except Exception as e:
                self.state = "error"
                log.exception("transcription failed for %s", group[0].name)
                db.log_event("error", f"transcribe {group[0].name}: {e}")
                for f in group:  # keep them aside so one bad file does not block the queue
                    f.replace(f.with_suffix(".failed"))
                time.sleep(1)

    def process_file(self, path: Path) -> dict | None:
        segs = self.process_group([path])
        return segs[0] if segs else None

    def process_group(self, paths: list[Path]) -> list[dict]:
        """Whisper always works on a 30 s window, so short pieces are joined into one window
        (with a little silence between) and transcribed in one pass. Each Whisper segment is then
        mapped back to the piece it came from, so every saved line keeps its real time."""
        gap = np.zeros(int(0.4 * RATE), dtype=np.float32)
        parts, spans, pos = [], [], 0.0
        for p in paths:
            a = load_wav(p)
            start, end, source = parse_name(p)
            spans.append((pos, pos + len(a) / RATE, start, end, source))
            parts += [a, gap]
            pos += (len(a) + len(gap)) / RATE
        audio = np.concatenate(parts)
        segs, lang = self.engine.transcribe_segments(audio)
        per: list[list[tuple]] = [[] for _ in paths]          # whisper segments of each piece
        for s0, s1, text, lp in segs:
            mid = (s0 + s1) / 2
            i = next((k for k, sp in enumerate(spans) if mid < sp[1] + 0.2), len(spans) - 1)
            per[i].append((s0, s1, text, lp))
        saved = []
        for i, p in enumerate(paths):
            p0, p1, start, end, source = spans[i]
            for run in self._speaker_runs(audio, p0, p1, per[i]):
                text = clean_text(" ".join(r[2] for r in run["segs"]))
                # real times: piece start + offset inside the piece
                r_start = start + max(0.0, run["segs"][0][0] - p0)
                r_end = min(end, start + max(0.0, run["segs"][-1][1] - p0))
                s_at, e_at = _stamp(r_start), _stamp(max(r_end, r_start))
                if not text or is_echo(source, text, s_at, e_at):
                    continue
                hint = heuristics.tag_line(text, datetime.fromtimestamp(start).date())
                logps = [r[3] for r in run["segs"]]
                seg = db.add_segment(s_at, e_at, source, text, lang, sum(logps) / len(logps) if logps else None,
                                     hint if hint["likely_todo"] or hint["priority"] else None,
                                     gap_min=float(self.cfg["conversation_gap_min"]),
                                     max_min=float(self.cfg.get("max_conversation_min", 20)),
                                     speaker_id=run["speaker"], speaker_score=run["score"])
                saved.append(seg)
                self.done_count += 1
                if self.on_segment:
                    self.on_segment(seg)
            if int(self.cfg.get("keep_audio_days", 0)) > 0:
                config.AUDIO_DIR.mkdir(parents=True, exist_ok=True)
                p.replace(config.AUDIO_DIR / p.name)
            else:
                p.unlink(missing_ok=True)
        if saved and self.voice_ready():
            from . import speakers
            if self.done_count % 10 < len(saved):
                try:
                    speakers.maybe_enroll_me(self.cfg.get("my_name") or "Me")
                except Exception:
                    log.exception("voice self-enrol failed")
        return saved

    # ------------------------------------------------------------ who is speaking
    def voice_ready(self) -> bool:
        if not self.cfg.get("voice_id", True):
            return False
        if self._voice is None:
            try:
                from .speakers import VoiceModel
                self._voice = VoiceModel()
            except Exception as e:
                log.warning("voice recognition off: %s", e)
                self._voice = False
        return bool(self._voice)

    def _speaker_runs(self, audio: np.ndarray, p0: float, p1: float, segs: list[tuple]) -> list[dict]:
        """Split one piece's Whisper segments into runs of the same speaker."""
        if not segs:
            return []
        if not self.voice_ready():
            return [{"speaker": None, "score": None, "segs": segs}]
        from . import speakers
        thr = float(self.cfg.get("speaker_threshold", 0.50))
        min_new = float(self.cfg.get("speaker_min_new_seconds", 2.5))
        prev = self._last_speaker

        def ident(emb, secs):
            return speakers.identify(emb, secs, thr, prev_id=prev, min_new_seconds=min_new)

        def clip(a, b):
            a, b = max(a, p0), min(b, p1)
            return audio[int(a * RATE): int(b * RATE)]

        def whole_piece():
            emb = self._voice.embed(clip(p0, p1))
            return ident(emb, p1 - p0) if emb is not None else (prev, None)

        if len(segs) == 1:
            sid, score = whole_piece()
            self._last_speaker = sid or prev
            return [{"speaker": sid, "score": score, "segs": segs}]
        runs: list[dict] = []
        pending: list[tuple] = []          # too short to recognise, before the first known voice
        for s in segs:
            emb = self._voice.embed(clip(s[0], s[1]))
            if emb is None:                 # short phrase: same speaker as just before
                (runs[-1]["segs"] if runs else pending).append(s)
                continue
            sid, score = ident(emb, s[1] - s[0])
            prev = sid or prev
            if runs and runs[-1]["speaker"] == sid:
                runs[-1]["segs"].append(s)
            else:
                runs.append({"speaker": sid, "score": score, "segs": [s]})
        if pending:
            if runs:
                runs[0]["segs"] = pending + runs[0]["segs"]
            else:
                sid, score = whole_piece()
                runs = [{"speaker": sid, "score": score, "segs": pending}]
        self._last_speaker = runs[-1]["speaker"] or self._last_speaker
        return runs


def pick_group(files: list[Path], max_s: float = 28.0, max_gap_s: float = 120.0) -> list[Path]:
    """The oldest file plus the next files of the same source that fit in one Whisper window."""
    first = files[0]
    s0, e0, src = parse_name(first)
    group, total, last_end = [first], e0 - s0, e0
    for f in files[1:]:
        s, e, source = parse_name(f)
        if source != src or s - last_end > max_gap_s or total + (e - s) + 0.4 > max_s:
            break
        group.append(f)
        total += (e - s) + 0.4
        last_end = e
    return group
