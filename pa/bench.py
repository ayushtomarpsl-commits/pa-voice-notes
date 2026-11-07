"""Accuracy test bench: which speech-to-text engine understands YOUR office Hinglish best?

Clips live in data/bench/clips/: <name>.wav (any PCM wav) + <name>.txt (the correct text, typed by you).

  python -m pa bench add FILE.wav [--text "correct text"]   add a clip (text can be filled in later)
  python -m pa bench collect --n 30                          copy kept audio pieces in, with draft text to correct
  python -m pa bench run [--engines local:small groq:whisper-large-v3 ...]

Scores: WER (word error rate, lower is better) and key-word recall (share of your important words,
from Settings > Words to expect, that came out right; higher is better). Writes an HTML report
to exports/ and prints a table.
"""
from __future__ import annotations

import html
import json
import re
import shutil
import time
import wave
from datetime import datetime
from pathlib import Path

import numpy as np

from . import config

DRAFT_MARK = "#DRAFT "   # first characters of a reference you have not corrected yet


def bench_dir() -> Path:
    d = config.DATA_DIR / "bench" / "clips"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ------------------------------------------------------------------ scoring
def normalize(text: str) -> list[str]:
    """Lower-case words without punctuation. Keeps Devanagari letters and signs."""
    text = text.lower().replace("’", "'")
    text = re.sub(r"[^\wऀ-ॿ' ]+", " ", text)
    return [w.strip("'") for w in text.split() if w.strip("'")]


def wer(ref: str, hyp: str) -> float:
    r, h = normalize(ref), normalize(hyp)
    if not r:
        return 0.0 if not h else 1.0
    prev = list(range(len(h) + 1))
    for i, rw in enumerate(r, 1):
        cur = [i] + [0] * len(h)
        for j, hw in enumerate(h, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (rw != hw))
        prev = cur
    return prev[-1] / len(r)


def key_words(cfg: dict) -> set[str]:
    words = set()
    for item in (cfg.get("vocabulary") or "").split(","):
        words.update(normalize(item))
    return {w for w in words if len(w) > 1}


def keyword_recall(ref: str, hyp: str, keys: set[str]) -> tuple[int, int]:
    """(found, expected) for key words that appear in the reference."""
    r, h = set(normalize(ref)), set(normalize(hyp))
    expected = r & keys
    return len(expected & h), len(expected)


# ------------------------------------------------------------------ clips
def read_any_wav(path: Path) -> np.ndarray:
    """Any audio file (wav of any kind, mp3, m4a ...) -> 16 kHz mono float32."""
    try:
        return _read_pcm_wav(path)
    except wave.Error:
        return _read_float_wav(path)


def _read_float_wav(path: Path) -> np.ndarray:
    """WAV with 32/64-bit float samples (format 3), which the stdlib wave module cannot read."""
    import struct
    data = path.read_bytes()
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError(f"{path.name}: not a WAV file (convert it to WAV first)")
    pos, fmt, samples = 12, None, None
    while pos + 8 <= len(data):
        cid, size = data[pos:pos + 4], struct.unpack("<I", data[pos + 4:pos + 8])[0]
        body = data[pos + 8:pos + 8 + size]
        if cid == b"fmt ":
            tag, ch, rate = struct.unpack("<HHI", body[:8])
            bits = struct.unpack("<H", body[14:16])[0]
            if tag == 0xFFFE and len(body) >= 26:   # WAVE_FORMAT_EXTENSIBLE: real tag in the sub-format GUID
                tag = struct.unpack("<H", body[24:26])[0]
            fmt = (tag, ch, rate, bits)
        elif cid == b"data":
            samples = body
        pos += 8 + size + (size & 1)
    if not fmt or samples is None or fmt[0] != 3 or fmt[3] not in (32, 64):
        raise ValueError(f"{path.name}: unsupported WAV format {fmt}")
    tag, ch, rate, bits = fmt
    x = np.frombuffer(samples[: len(samples) // (bits // 8) * (bits // 8)],
                      dtype="<f4" if bits == 32 else "<f8").astype(np.float32)
    if ch > 1:
        x = x[: len(x) // ch * ch].reshape(-1, ch).mean(axis=1)
    if rate != 16000:
        import soxr
        x = soxr.resample(x, rate, 16000).astype(np.float32)
    return x


def _read_pcm_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        rate, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if width == 2:
        x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 4:
        x = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    elif width == 1:
        x = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128) / 128.0
    else:
        raise ValueError(f"{path.name}: unsupported sample width {width}")
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    if rate != 16000:
        import soxr
        x = soxr.resample(x, rate, 16000).astype(np.float32)
    return x


def add_clip(src: Path, text: str | None = None, name: str | None = None) -> Path:
    from .transcribe import RATE
    name = name or src.stem
    dst = bench_dir() / f"{name}.wav"
    audio = read_any_wav(src)
    with wave.open(str(dst), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())
    ref = dst.with_suffix(".txt")
    if text:
        ref.write_text(text.strip(), encoding="utf-8")
    elif not ref.exists():
        ref.write_text(DRAFT_MARK + "(type the correct words here)", encoding="utf-8")
    return dst


def collect(n: int = 30, engine=None) -> list[Path]:
    """Copy up to n kept audio pieces (Settings > Keep audio days must be > 0) into the bench, each
    with a draft text from the current model for you to correct."""
    pool = sorted(config.AUDIO_DIR.glob("*.wav"), key=lambda p: p.stat().st_mtime, reverse=True)
    pool = [p for p in pool if 3 <= _dur(p) <= 28][: n]
    out = []
    for p in pool:
        name = "clip_" + p.stem.replace(".", "_")
        if (bench_dir() / f"{name}.wav").exists():
            continue
        dst = add_clip(p, name=name)
        if engine is not None:
            segs, _ = engine.transcribe_segments(read_any_wav(dst))
            dst.with_suffix(".txt").write_text(DRAFT_MARK + " ".join(s[2] for s in segs), encoding="utf-8")
        out.append(dst)
    return out


def _dur(p: Path) -> float:
    try:
        with wave.open(str(p), "rb") as w:
            return w.getnframes() / w.getframerate()
    except Exception:
        return 0.0


def load_clips() -> list[dict]:
    clips = []
    for wav in sorted(bench_dir().glob("*.wav")):
        ref = wav.with_suffix(".txt")
        text = ref.read_text(encoding="utf-8").strip() if ref.exists() else ""
        clips.append({"name": wav.stem, "wav": wav, "ref": text.removeprefix(DRAFT_MARK).strip(),
                      "corrected": bool(text) and not text.startswith(DRAFT_MARK), "seconds": _dur(wav)})
    return clips


# ------------------------------------------------------------------ run
def default_engines(cfg: dict) -> list[str]:
    from . import keys
    from .transcribe import LOCAL_MODELS
    eng = ["local:small"]
    for name, path in LOCAL_MODELS.items():
        if Path(path).exists() or _hf_cached(path):
            eng.append(f"local:{name}")
    if keys.get("groq"):
        eng += ["groq:whisper-large-v3", "groq:whisper-large-v3-turbo"]
    return eng


def _hf_cached(repo: str) -> bool:
    if "/" not in repo or Path(repo).exists():
        return False
    d = Path.home() / ".cache" / "huggingface" / "hub" / ("models--" + repo.replace("/", "--"))
    return any(d.glob("snapshots/*/model.bin"))


def run(engines: list[str] | None = None, include_drafts: bool = False, engine_obj=None, log=print) -> dict:
    from .transcribe import Engine
    cfg = config.load()
    engines = engines or default_engines(cfg)
    clips = [c for c in load_clips() if c["corrected"] or include_drafts]
    if not clips:
        raise RuntimeError(f"No corrected clips in {bench_dir()}. Add clips and type the correct text in the .txt files.")
    keys_ = key_words(cfg)
    eng = engine_obj or Engine(cfg)
    results = {e: {"rows": [], "wer_sum": 0.0, "words": 0, "kw_found": 0, "kw_total": 0, "secs": 0.0,
                   "audio": 0.0, "errors": 0} for e in engines}
    for e in engines:
        log(f"== {e}")
        for c in clips:
            audio = read_any_wav(c["wav"])
            t0 = time.time()
            try:
                segs, _ = eng.transcribe_segments(audio, engine=e)
                hyp = " ".join(s[2] for s in segs)
                err = None
            except Exception as ex:
                hyp, err = "", str(ex)
            took = time.time() - t0
            n = len(normalize(c["ref"]))
            w = wer(c["ref"], hyp) if err is None else 1.0
            kf, kt = keyword_recall(c["ref"], hyp, keys_)
            r = results[e]
            r["rows"].append({"clip": c["name"], "ref": c["ref"], "hyp": hyp, "wer": w, "kw": [kf, kt],
                              "secs": took, "error": err})
            r["wer_sum"] += w * n
            r["words"] += n
            r["kw_found"] += kf
            r["kw_total"] += kt
            r["secs"] += took
            r["audio"] += c["seconds"]
            r["errors"] += err is not None
            log(f"   {c['name']}: WER {w:.0%}  key words {kf}/{kt}  {took:.1f}s" + (f"  ERROR {err}" if err else ""))
            if e.startswith("groq:"):
                time.sleep(3.2)   # free plan: 20 requests per minute
    summary = []
    for e, r in results.items():
        summary.append({"engine": e, "wer": r["wer_sum"] / max(1, r["words"]),
                        "keyword_recall": (r["kw_found"] / r["kw_total"]) if r["kw_total"] else None,
                        "speed_x_realtime": (r["audio"] / r["secs"]) if r["secs"] else None,
                        "errors": r["errors"], "clips": len(r["rows"])})
    summary.sort(key=lambda s: (s["errors"] > 0, s["wer"]))
    report = {"at": datetime.now().isoformat(timespec="seconds"), "clips": len(clips),
              "audio_seconds": sum(c["seconds"] for c in clips), "summary": summary, "details": results}
    report["html"] = write_html(report)
    return report


def write_html(report: dict) -> str:
    config.EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = report["at"].replace(":", "").replace("-", "")
    path = config.EXPORT_DIR / f"bench-{stamp}.html"
    e = html.escape

    def pct(v):
        return "–" if v is None else f"{v:.0%}"

    rows = "".join(
        f"<tr class='{'best' if i == 0 else ''}'><td>{e(s['engine'])}</td><td>{pct(s['wer'])}</td>"
        f"<td>{pct(s['keyword_recall'])}</td><td>{'' if s['speed_x_realtime'] is None else f'{s['speed_x_realtime']:.1f}x'}</td>"
        f"<td>{s['errors']}</td></tr>" for i, s in enumerate(report["summary"]))
    detail = ""
    for eng, r in report["details"].items():
        detail += f"<h3>{e(eng)}</h3><table><tr><th>Clip</th><th>WER</th><th>Key words</th><th>Correct text</th><th>Engine heard</th></tr>"
        for row in r["rows"]:
            detail += (f"<tr><td>{e(row['clip'])}</td><td>{row['wer']:.0%}</td><td>{row['kw'][0]}/{row['kw'][1]}</td>"
                       f"<td>{e(row['ref'])}</td><td>{e(row['hyp'] or row['error'] or '')}</td></tr>")
        detail += "</table>"
    page = f"""<!doctype html><html><head><meta charset="utf-8"><title>PA accuracy test</title>
<style>body{{font:14px/1.45 "Segoe UI",system-ui,sans-serif;margin:24px;color:#1f2328;background:#f6f5f2}}
table{{border-collapse:collapse;background:#fff;margin:8px 0 20px;width:100%}}td,th{{border:1px solid #e4e2dc;padding:6px 8px;text-align:left;vertical-align:top}}
th{{background:#f0eee9}}tr.best td{{background:#e9f7ec;font-weight:600}}.muted{{color:#6b7280}}</style></head><body>
<h1>Speech-to-text accuracy test</h1>
<p class="muted">{e(report['at'])} · {report['clips']} clips · {report['audio_seconds']/60:.1f} min of audio.
WER = share of words wrong (lower is better). Key words = your important words heard right (higher is better).
Speed = how many times faster than real time.</p>
<table><tr><th>Engine</th><th>WER</th><th>Key words</th><th>Speed</th><th>Errors</th></tr>{rows}</table>
<p class="muted">Note: if the correct text is in English letters (Hinglish) but an engine writes Hindi in Devanagari,
its WER looks worse than its real quality. Read the details below.</p>
{detail}</body></html>"""
    path.write_text(page, encoding="utf-8")
    (path.with_suffix(".json")).write_text(json.dumps({k: v for k, v in report.items() if k != "html"},
                                                      indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    return str(path)
