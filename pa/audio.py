"""Audio capture. Two sources, each in its own thread:
  'me'   = the microphone (earphone mic or laptop mic)
  'call' = what the PC plays (WASAPI loopback) = the other side of Teams/Meet/Zoom calls

Each source converts audio to 16 kHz mono float32 and hands it to `on_audio(source, samples, t_end)`.
If a device disappears (earphones unplugged, Bluetooth drops, default device changed) the source
re-opens with backoff. While paused, streams are closed so Windows shows the mic as not in use.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Callable

import numpy as np
import pyaudiowpatch as pyaudio
import soxr

log = logging.getLogger("pa.audio")
TARGET_RATE = 16000
OnAudio = Callable[[str, np.ndarray, float], None]

# PortAudio crashes (access violation) if two threads start/stop it at the same time, so every
# PyAudio()/open/close/terminate goes through this lock. PortAudio also only re-reads the device
# list when no instance is alive, so a re-open waits until all sources have closed (`_alive`).
_PA_LOCK = threading.RLock()
_alive = 0
_alive_cond = threading.Condition(_PA_LOCK)
_fresh_ok = threading.Event()


def _pa_open() -> pyaudio.PyAudio:
    global _alive
    with _alive_cond:
        _alive_cond.wait_for(lambda: _alive == 0 or _fresh_ok.is_set(), timeout=4)
        _fresh_ok.set()   # first instance after a full close has fresh devices; others may join
        _alive += 1
        return pyaudio.PyAudio()


def _pa_close(p: pyaudio.PyAudio) -> None:
    global _alive
    with _alive_cond:
        try:
            p.terminate()
        finally:
            _alive -= 1
            if _alive == 0:
                _fresh_ok.clear()
            _alive_cond.notify_all()



def default_names() -> tuple[str | None, str | None]:
    """(default mic, default speaker) read fresh from Windows. Used to notice new earphones."""
    try:
        import soundcard as sc
        return sc.default_microphone().name, sc.default_speaker().name
    except Exception:
        return None, None


class DeviceWatcher(threading.Thread):
    """Every few seconds: if the Windows default mic/speaker changed, re-open all sources."""

    def __init__(self, sources: list, every: float = 4.0):
        super().__init__(daemon=True, name="device-watch")
        self.sources, self.every = sources, every
        self._stop = threading.Event()
        self.current = default_names()

    def run(self) -> None:
        while not self._stop.wait(self.every):
            now = default_names()
            if now != (None, None) and now != self.current:
                log.info("default audio devices changed: %s -> %s", self.current, now)
                self.current = now
                with _alive_cond:
                    _fresh_ok.clear()   # make every source wait for a full PortAudio restart
                for s in self.sources:
                    s.reopen()

    def stop(self) -> None:
        self._stop.set()


def list_devices() -> dict:
    """Input microphones and loopback devices (WASAPI only, which is what we capture with)."""
    out = {"inputs": [], "loopbacks": [], "default_input": None, "default_loopback": None}
    mic, spk = default_names()
    out["windows_default_mic"], out["windows_default_speaker"] = mic, spk
    with _PA_LOCK, pyaudio.PyAudio() as p:
        for d in p.get_device_info_generator_by_host_api(host_api_type=pyaudio.paWASAPI):
            item = {"index": d["index"], "name": d["name"], "channels": d["maxInputChannels"],
                    "rate": int(d["defaultSampleRate"])}
            if d.get("isLoopbackDevice"):
                out["loopbacks"].append(item)
            elif d["maxInputChannels"] > 0:
                out["inputs"].append(item)
        try:
            out["default_input"] = p.get_default_wasapi_device(d_in=True)["name"]
        except Exception:
            pass
        try:
            out["default_loopback"] = p.get_default_wasapi_loopback()["name"]
        except Exception:
            pass
    return out


def _find_device(p: pyaudio.PyAudio, kind: str, name_hint: str) -> dict:
    """kind 'me' -> mic (by name hint, else Windows default); 'call' -> loopback of default speakers."""
    if kind == "call":
        return p.get_default_wasapi_loopback()
    if name_hint:
        hint = name_hint.lower()
        for d in p.get_device_info_generator_by_host_api(host_api_type=pyaudio.paWASAPI):
            if d["maxInputChannels"] > 0 and not d.get("isLoopbackDevice") and hint in d["name"].lower():
                return d
        log.warning("mic %r not found, using Windows default", name_hint)
    return p.get_default_wasapi_device(d_in=True)


class AudioSource(threading.Thread):
    def __init__(self, kind: str, on_audio: OnAudio, mic_hint: str = ""):
        super().__init__(daemon=True, name=f"audio-{kind}")
        self.kind = kind
        self.on_audio = on_audio
        self.mic_hint = mic_hint
        self.device_name: str | None = None
        self.state = "stopped"          # stopped | paused | starting | listening | error
        self.error: str | None = None
        self.level = 0.0                # recent loudness 0..1, for the UI meter
        self._q: queue.Queue = queue.Queue(maxsize=2000)
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._reopen = threading.Event()

    # ---- control (any thread)
    def pause(self, on: bool) -> None:
        if on:
            self._paused.set()
        else:
            self._paused.clear()
        self._reopen.set()

    def reopen(self) -> None:
        """Ask the source to re-open its device (after sleep/resume or a device change)."""
        self._reopen.set()

    def stop(self) -> None:
        self._stop.set()
        self._reopen.set()

    # ---- thread
    def run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            if self._paused.is_set():
                self.state = "paused"
                self._reopen.wait(1.0)
                self._reopen.clear()
                continue
            try:
                self._run_stream()
                backoff = 1.0
            except Exception as e:  # device gone, driver error, exclusive mode ...
                self.state, self.error = "error", str(e)
                log.warning("%s source error: %s (retry in %.0fs)", self.kind, e, backoff)
                self._reopen.wait(backoff)
                self._reopen.clear()
                backoff = min(backoff * 2, 30.0)
        self.state = "stopped"

    def _run_stream(self) -> None:
        self.state, self.error = "starting", None
        self._reopen.clear()
        p = _pa_open()
        stream = None
        try:
            with _PA_LOCK:
                dev = _find_device(p, self.kind, self.mic_hint)
                rate, ch = int(dev["defaultSampleRate"]), max(1, int(dev["maxInputChannels"]))
                self.device_name = dev["name"]
                block = rate // 10  # 100 ms

                def callback(in_data, frame_count, time_info, status):
                    try:
                        self._q.put_nowait((in_data, time.time()))
                    except queue.Full:
                        pass  # consumer is stuck; drop rather than block the audio driver
                    return (None, pyaudio.paContinue)

                stream = p.open(format=pyaudio.paInt16, channels=ch, rate=rate, input=True,
                                input_device_index=dev["index"], frames_per_buffer=block,
                                stream_callback=callback)
            resampler = soxr.ResampleStream(rate, TARGET_RATE, 1, dtype="float32")
            log.info("%s listening on %s (%d Hz, %d ch)", self.kind, self.device_name, rate, ch)
            self.state = "listening"
            while not self._stop.is_set() and not self._reopen.is_set():
                try:
                    data, t = self._q.get(timeout=0.5)
                except queue.Empty:
                    data = None
                if data is not None:
                    x = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
                    if ch > 1:
                        x = x.reshape(-1, ch).mean(axis=1)
                    self.level = float(min(1.0, np.sqrt(np.mean(x * x)) * 8)) if x.size else 0.0
                    y = resampler.resample_chunk(x)
                    if y.size:
                        self.on_audio(self.kind, y, t)
                else:
                    self.level = 0.0
                if not stream.is_active():
                    raise RuntimeError("stream stopped (device removed?)")
        finally:
            with _PA_LOCK:
                if stream is not None:
                    try:
                        stream.stop_stream()
                        stream.close()
                    except Exception:
                        pass
            _pa_close(p)
            self.level = 0.0
            while not self._q.empty():
                self._q.get_nowait()
