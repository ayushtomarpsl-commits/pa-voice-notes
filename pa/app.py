"""The running assistant: wires audio -> VAD -> transcription queue -> notes, and owns pause state.

Listening stops (streams closed) when any of these is true:
  - you paused it (button, tray, or the Ctrl+Alt+P hotkey) or turned on private mode
  - it is outside your work hours / in your lunch break (if enabled)
  - the PC is asleep or the screen is locked
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta

from . import config, db, extract, keys
from .audio import AudioSource, DeviceWatcher
from .transcribe import Transcriber
from .vad import Segmenter
from .winops import WinEvents, power_status

log = logging.getLogger("pa.app")


def _in_window(now: datetime, start: str, end: str) -> bool:
    s, e = now.strftime("%H:%M") >= start, now.strftime("%H:%M") < end
    return (s and e) if start <= end else (s or e)


def schedule_reason(cfg: dict, now: datetime | None = None) -> str | None:
    """Why listening should be off right now because of the schedule, or None."""
    now = now or datetime.now()
    wh = cfg.get("work_hours") or {}
    if wh.get("enabled"):
        if now.weekday() not in wh.get("days", []) or not _in_window(now, wh["start"], wh["end"]):
            return "outside work hours"
    lunch = cfg.get("lunch") or {}
    if lunch.get("enabled") and _in_window(now, lunch["start"], lunch["end"]):
        return "lunch break"
    return None


class Assistant:
    def __init__(self, cfg: dict | None = None, listen: bool = True):
        self.cfg = cfg or config.load()
        self.listen = listen
        self.manual_pause = False
        self.private = False
        self.system_off: set[str] = set()   # 'sleep', 'locked'
        self.schedule_off: str | None = None
        self.lock = threading.Lock()
        self.transcriber = Transcriber(self.cfg)
        self.segmenters: dict[str, Segmenter] = {}
        self.seg_locks: dict[str, threading.Lock] = {}
        self.sources: dict[str, AudioSource] = {}
        self.events: WinEvents | None = None
        self._stop = threading.Event()
        self.last_process: dict | None = None
        self.on_change = None   # tray sets this to refresh its icon

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        config.ensure_dirs()
        db.conn()
        self.transcriber.start()
        if self.listen:
            kinds = ["me"] + (["call"] if self.cfg.get("capture_calls", True) else [])
            for k in kinds:
                c = self.cfg
                self.segmenters[k] = Segmenter(k, c["vad_threshold"], c["min_speech_ms"],
                                               c["silence_end_ms"], c["max_utterance_s"])
                self.seg_locks[k] = threading.Lock()
                self.sources[k] = AudioSource(k, self._on_audio, c.get("mic_device", "") if k == "me" else "")
            self._apply_pause()
            for s in self.sources.values():
                s.start()
            self.watcher = DeviceWatcher(list(self.sources.values()))
            self.watcher.start()
            self.events = WinEvents(self._on_win_event, self.cfg.get("hotkey", "ctrl+alt+p"))
            self.events.start()
        threading.Thread(target=self._scheduler, daemon=True, name="scheduler").start()
        db.log_event("start", f"listen={self.listen}")

    def stop(self) -> None:
        self._stop.set()
        for k, s in self.sources.items():
            s.stop()
            self._flush(k)
        if self.events:
            self.events.stop()
        self.transcriber.stop()
        db.log_event("stop")

    # ------------------------------------------------------------ audio path
    def _on_audio(self, kind: str, samples, t_end: float) -> None:
        with self.seg_locks[kind]:
            utterances = self.segmenters[kind].feed(samples, t_end)
        for u in utterances:
            self.transcriber.submit(u)

    def _flush(self, kind: str) -> None:
        with self.seg_locks[kind]:
            utterances = self.segmenters[kind].flush()
        for u in utterances:
            self.transcriber.submit(u)

    # ------------------------------------------------------------ pause logic
    @property
    def paused(self) -> bool:
        return self.manual_pause or self.private or bool(self.system_off) or bool(self.schedule_off)

    def pause_reason(self) -> str | None:
        if self.private:
            return "private mode"
        if self.manual_pause:
            return "paused by you"
        if self.system_off:
            return ", ".join(sorted(self.system_off))
        return self.schedule_off

    def _apply_pause(self) -> None:
        on = self.paused
        for k, s in self.sources.items():
            if on and s.state != "paused":
                self._flush(k)
            s.pause(on)
        if self.on_change:
            try:
                self.on_change()
            except Exception:
                pass

    def set_paused(self, on: bool) -> None:
        with self.lock:
            if self.manual_pause != on:
                self.manual_pause = on
                db.log_event("pause" if on else "resume", "manual")
            self._apply_pause()

    def set_private(self, on: bool) -> None:
        with self.lock:
            if self.private != on:
                self.private = on
                db.log_event("private_on" if on else "private_off")
            self._apply_pause()

    def toggle(self) -> None:
        self.set_paused(not self.manual_pause)

    def _on_win_event(self, ev: str) -> None:
        with self.lock:
            if ev == "hotkey":
                self.manual_pause = not self.manual_pause
                db.log_event("pause" if self.manual_pause else "resume", "hotkey")
            elif ev == "suspend":
                self.system_off.add("sleep")
            elif ev == "resume":
                self.system_off.discard("sleep")
                for s in self.sources.values():
                    s.reopen()   # devices may have changed while asleep
            elif ev == "lock":
                self.system_off.add("locked")
            elif ev == "unlock":
                self.system_off.discard("locked")
            elif ev == "power":
                self.transcriber.wake.set()
            self._apply_pause()

    # ------------------------------------------------------------ background jobs
    def _scheduler(self) -> None:
        last_cleanup = 0.0
        while not self._stop.wait(30):
            try:
                self.cfg = config.load()
                self.transcriber.cfg.update(self.cfg)
                reason = schedule_reason(self.cfg)
                if reason != self.schedule_off:
                    with self.lock:
                        self.schedule_off = reason
                        db.log_event("auto_pause" if reason else "auto_resume", reason)
                        self._apply_pause()
                db.close_idle_conversations(float(self.cfg["conversation_gap_min"]))
                if self.cfg.get("auto_process") and extract.ready(self.cfg) and self.transcriber.queue_size() == 0:
                    pending = db.list_conversations(status="ended")
                    if pending:
                        self.last_process = extract.process_pending(self.cfg)
                if time.time() - last_cleanup > 3600:
                    last_cleanup = time.time()
                    self._cleanup_audio()
            except Exception:
                log.exception("scheduler tick failed")

    def _cleanup_audio(self) -> None:
        days = int(self.cfg.get("keep_audio_days", 0))
        cutoff = (datetime.now() - timedelta(days=days)).timestamp()
        for f in config.AUDIO_DIR.glob("*.wav"):
            if f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)

    def process_now(self, day: str | None = None) -> dict:
        db.close_idle_conversations(0)   # treat everything said so far as finished
        self.last_process = extract.process_pending(self.cfg, day, respect_pause=False)
        return self.last_process

    # ------------------------------------------------------------ status for UI / tray
    def status(self) -> dict:
        return {
            "listening": self.listen and not self.paused,
            "paused": self.paused,
            "pause_reason": self.pause_reason(),
            "manual_pause": self.manual_pause,
            "private": self.private,
            "sources": [{"kind": k, "device": s.device_name, "state": s.state, "error": s.error,
                         "level": round(s.level, 3)} for k, s in self.sources.items()],
            "transcriber": {"state": self.transcriber.state, "queue": self.transcriber.queue_size(),
                            "model": self.transcriber.engine.last_engine or (
                                "groq" if self.transcriber.engine.cloud_ready() else f"local:{self.cfg['whisper_model']}"),
                            "error": self.transcriber.engine.last_error, "done": self.transcriber.done_count},
            "power": power_status(),
            "api_key_set": extract.ready(self.cfg),
            "keys": keys.status(),
            "notes_engine": self.cfg.get("llm_engine"),
            "hotkey": self.cfg.get("hotkey"),
            "hotkey_ok": bool(self.events and self.events.hotkey_ok),
            "usage_today": db.usage_today(),
            "today": db.today(),
        }
