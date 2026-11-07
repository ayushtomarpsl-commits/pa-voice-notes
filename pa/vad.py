"""Speech detection. Uses the Silero VAD model that ships inside faster-whisper (no download).

`Segmenter.feed(samples, t_end)` takes 16 kHz mono audio and returns finished utterances:
pieces of speech with their start/end time, ready for Whisper. Silence is thrown away here,
so nothing but speech is ever written to disk.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import numpy as np
import onnxruntime as ort

RATE = 16000
FRAME = 512          # 32 ms per VAD step
CONTEXT = 64


def _model_path() -> str:
    import faster_whisper
    return os.path.join(os.path.dirname(faster_whisper.__file__), "assets", "silero_vad_v6.onnx")


_SESSION: ort.InferenceSession | None = None


def _session() -> ort.InferenceSession:
    global _SESSION
    if _SESSION is None:
        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        opts.log_severity_level = 4
        _SESSION = ort.InferenceSession(_model_path(), providers=["CPUExecutionProvider"], sess_options=opts)
    return _SESSION


class StreamingVAD:
    """Speech probability per 32 ms frame, keeping the model state between calls."""

    def __init__(self):
        self.sess = _session()
        self.reset()

    def reset(self) -> None:
        self.h = np.zeros((1, 1, 128), dtype=np.float32)
        self.c = np.zeros((1, 1, 128), dtype=np.float32)
        self.ctx = np.zeros(CONTEXT, dtype=np.float32)

    def prob(self, frame: np.ndarray) -> float:
        x = np.concatenate([self.ctx, frame]).astype(np.float32)[None, :]
        out, self.h, self.c = self.sess.run(None, {"input": x, "h": self.h, "c": self.c})
        self.ctx = frame[-CONTEXT:]
        return float(out[0])


@dataclass
class Utterance:
    source: str
    start: float      # epoch seconds
    end: float
    audio: np.ndarray  # 16 kHz mono float32


class Segmenter:
    def __init__(self, source: str, threshold: float = 0.5, min_speech_ms: int = 600,
                 silence_end_ms: int = 900, max_utterance_s: float = 28, pre_roll_ms: int = 300):
        self.source = source
        self.vad = StreamingVAD()
        self.on_thr, self.off_thr = threshold, max(0.05, threshold - 0.15)
        self.min_frames = max(1, min_speech_ms * RATE // 1000 // FRAME)
        self.end_frames = max(1, silence_end_ms * RATE // 1000 // FRAME)
        self.max_frames = int(max_utterance_s * RATE / FRAME)
        self.pre_frames = max(1, pre_roll_ms * RATE // 1000 // FRAME)
        self.buf = np.zeros(0, dtype=np.float32)
        self.pre: list[np.ndarray] = []
        self.frames: list[np.ndarray] = []
        self.speech_frames = 0
        self.silence = 0
        self.active = False
        self.start_t = 0.0
        self.t = 0.0  # epoch time of the end of the last processed frame

    def flush(self) -> list[Utterance]:
        """End any open utterance (on pause/stop)."""
        out = self._finish()
        self.vad.reset()
        self.buf = np.zeros(0, dtype=np.float32)
        self.pre = []
        return [out] if out else []

    def feed(self, samples: np.ndarray, t_end: float) -> list[Utterance]:
        out: list[Utterance] = []
        self.buf = np.concatenate([self.buf, samples.astype(np.float32)])
        n = len(self.buf) // FRAME
        if n == 0:
            return out
        # time of the first frame we are about to process
        t0 = t_end - len(self.buf) / RATE
        for i in range(n):
            frame = self.buf[i * FRAME:(i + 1) * FRAME]
            self.t = t0 + (i + 1) * FRAME / RATE
            p = self.vad.prob(frame)
            if not self.active:
                self.pre.append(frame)
                if len(self.pre) > self.pre_frames:
                    self.pre.pop(0)
                if p >= self.on_thr:
                    self.active = True
                    self.frames = list(self.pre)
                    self.pre = []
                    self.start_t = self.t - len(self.frames) * FRAME / RATE
                    self.speech_frames, self.silence = 1, 0
            else:
                self.frames.append(frame)
                if p >= self.off_thr:
                    self.speech_frames += 1
                    self.silence = 0
                else:
                    self.silence += 1
                if self.silence >= self.end_frames or len(self.frames) >= self.max_frames:
                    u = self._finish()
                    if u:
                        out.append(u)
        self.buf = self.buf[n * FRAME:]
        return out

    def _finish(self) -> Utterance | None:
        if not self.active:
            return None
        self.active = False
        frames, speech = self.frames, self.speech_frames
        self.frames, self.speech_frames, self.silence = [], 0, 0
        if speech < self.min_frames:
            return None
        audio = np.concatenate(frames)
        return Utterance(self.source, self.start_t, self.start_t + len(audio) / RATE, audio)
