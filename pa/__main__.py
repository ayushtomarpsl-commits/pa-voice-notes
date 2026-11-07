"""Command line.

  python -m pa run              start listening + web page + tray icon (normal daily use)
  python -m pa run --no-tray    same, without the tray icon (Ctrl+C to stop)
  python -m pa serve            only the web page (no listening) - to read notes
  python -m pa mcp              MCP server for Claude Code (stdio)
  python -m pa devices          list microphones and PC-sound devices
  python -m pa add-audio FILE   transcribe a 16 kHz mono WAV as if it was heard now (testing / old recordings)
  python -m pa process [--day]  turn finished conversations into notes (needs API key)
  python -m pa export [--day]   write exports/<day>.md for Claude Code
  python -m pa bench add|collect|list|run   accuracy test bench (see pa/bench.py)
"""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import sys
import threading
import time

from . import config


ALREADY_RUNNING = 3


def setup_logging(to_console: bool = True) -> None:
    config.ensure_dirs()
    handlers: list[logging.Handler] = [logging.handlers.RotatingFileHandler(
        config.LOG_DIR / "pa.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")]
    if to_console and sys.stderr:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def start_server(assistant, port: int):
    import uvicorn
    from .server import create_app
    cfg = uvicorn.Config(create_app(assistant), host="127.0.0.1", port=port, log_level="warning",
                         log_config=None)  # use our log file, never a console
    srv = uvicorn.Server(cfg)
    threading.Thread(target=srv.run, daemon=True, name="web").start()
    return srv


def cmd_run(args) -> int:
    from .app import Assistant
    from .winops import set_low_priority, single_instance
    setup_logging(to_console=not args.quiet)
    log = logging.getLogger("pa")
    handle = single_instance()
    if handle is None:
        print("PA is already running. Open http://127.0.0.1:%d" % config.load()["port"])
        return ALREADY_RUNNING
    cfg = config.load()
    if cfg.get("low_priority", True):
        set_low_priority()
    assistant = Assistant(cfg, listen=not args.no_listen)
    assistant.start()
    port = cfg["port"]
    srv = start_server(assistant, port)
    url = f"http://127.0.0.1:{port}/"
    log.info("PA running: %s", url)
    stop = threading.Event()

    def quit_all():
        stop.set()
        assistant.stop()
        srv.should_exit = True

    if not args.no_tray:
        from .tray import run_tray
        run_tray(assistant, url, quit_all)
    else:
        print(f"PA is listening. My Day page: {url}  (Ctrl+C to stop)")
        try:
            while not stop.is_set():
                time.sleep(1)
        except KeyboardInterrupt:
            quit_all()
    time.sleep(1)  # let the transcriber finish the current piece
    return 0


def cmd_supervise(args) -> int:
    """Keeper used by the login shortcut: runs `pa run` and starts it again if it stops by
    itself (crash). Quitting from the tray icon exits with code 0 and stops the keeper too."""
    import subprocess
    setup_logging(to_console=False)
    log = logging.getLogger("pa.keeper")
    exe = sys.executable
    fails = 0
    while True:
        started = time.time()
        code = subprocess.call([exe, "-m", "pa", "run", "--quiet"], cwd=str(config.ROOT))
        if code in (0, ALREADY_RUNNING):   # 0 = you quit from the tray
            log.info("PA exited with code %s; keeper stops", code)
            return code
        fails = fails + 1 if time.time() - started < 120 else 1
        wait = min(300, 5 * 2 ** (fails - 1))
        log.warning("PA stopped unexpectedly (code %s); restarting in %ss", code, wait)
        time.sleep(wait)


def cmd_serve(args) -> int:
    import uvicorn
    from .server import create_app
    setup_logging()
    port = config.load()["port"]
    print(f"Notes page (not listening): http://127.0.0.1:{port}/")
    uvicorn.run(create_app(None), host="127.0.0.1", port=port, log_level="warning")
    return 0


def cmd_devices(args) -> int:
    from .audio import list_devices
    print(json.dumps(list_devices(), indent=2, ensure_ascii=False))
    return 0


def cmd_add_audio(args) -> int:
    """Run a WAV through the same VAD + Whisper path as live audio."""
    import numpy as np
    from .transcribe import Transcriber, load_wav
    from .vad import RATE, Segmenter
    setup_logging()
    cfg = config.load()
    audio = load_wav(__import__("pathlib").Path(args.file))
    seg = Segmenter(args.source, cfg["vad_threshold"], cfg["min_speech_ms"], cfg["silence_end_ms"], cfg["max_utterance_s"])
    t0 = time.time() - len(audio) / RATE
    utts = []
    for i in range(0, len(audio), 1600):
        chunk = audio[i:i + 1600]
        utts += seg.feed(chunk, t0 + (i + len(chunk)) / RATE)
    utts += seg.flush()
    from .transcribe import pick_group, save_utterance
    tr = Transcriber(cfg)
    folder = config.DATA_DIR / "import"
    paths = [save_utterance(u, folder) for u in utts]
    t = time.time()
    while paths:
        group = pick_group(paths)
        paths = paths[len(group):]
        for s in tr.process_group(group):
            print(f"[{s['started_at'][11:19]} {s['source']}] {s['text']}")
    took = time.time() - t
    print(f"\n{len(utts)} pieces, {len(audio)/RATE:.0f}s audio, transcribed in {took:.1f}s "
          f"(speed {len(audio)/RATE/max(took, 0.01):.1f}x real time)")
    return 0


def cmd_process(args) -> int:
    from . import db, extract
    setup_logging()
    db.close_idle_conversations(0)
    print(json.dumps(extract.process_pending(config.load(), args.day), indent=2))
    return 0


def cmd_export(args) -> int:
    from . import handoff
    print(handoff.write(args.day))
    return 0


def cmd_bench(args) -> int:
    from pathlib import Path
    from . import bench
    setup_logging(to_console=False)
    if args.action == "add":
        p = bench.add_clip(Path(args.file), args.text)
        print(f"Added {p}" + ("" if args.text else f"\nNow type the correct words in {p.with_suffix('.txt')}"))
    elif args.action == "collect":
        from .transcribe import Engine
        cfg = config.load()
        got = bench.collect(args.n, Engine({**cfg, "stt_engine": "local"}))
        print(f"Added {len(got)} clips to {bench.bench_dir()}. Correct each .txt file (remove '#DRAFT ').")
        if not got:
            print("No kept audio found. Set Settings > Keep audio (days) to 2, talk for a day, then run again.")
    elif args.action == "list":
        for c in bench.load_clips():
            print(f"{'ok   ' if c['corrected'] else 'DRAFT'} {c['seconds']:5.1f}s {c['name']}: {c['ref'][:70]}")
    else:
        r = bench.run(args.engines or None, include_drafts=args.drafts)
        print("\nRESULT (best first):")
        for s in r["summary"]:
            kw = "–" if s["keyword_recall"] is None else f"{s['keyword_recall']:.0%}"
            sp = "–" if s["speed_x_realtime"] is None else f"{s['speed_x_realtime']:.1f}x"
            print(f"  {s['engine']:<32} WER {s['wer']:.0%}   key words {kw}   speed {sp}   errors {s['errors']}")
        print(f"\nReport: {r['html']}")
    return 0


def cmd_mcp(args) -> int:
    from .mcp_server import main
    main()
    return 0


def main(argv=None) -> int:
    # pythonw.exe (used by the login shortcut) has no console: sys.stdout/stderr are None and any
    # library that writes to them (uvicorn logging, tqdm) would crash PA at start.
    import os
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        config.ensure_dirs()
        sys.stderr = open(config.LOG_DIR / "stderr.txt", "a", encoding="utf-8", buffering=1)
    # if the process ever dies inside native code (audio driver, speech model), write where
    import faulthandler
    config.ensure_dirs()
    _crash_log = open(config.LOG_DIR / "crash.txt", "a", encoding="utf-8", buffering=1)
    faulthandler.enable(file=_crash_log, all_threads=True)
    p = argparse.ArgumentParser(prog="pa", description="Personal assistant: listen, transcribe, take notes.")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--no-tray", action="store_true")
    r.add_argument("--no-listen", action="store_true", help="web page + processing only")
    r.add_argument("--quiet", action="store_true", help="no console log (used by autostart)")
    r.set_defaults(fn=cmd_run)
    sub.add_parser("serve").set_defaults(fn=cmd_serve)
    sub.add_parser("supervise", help="run PA and restart it if it crashes (used at login)").set_defaults(fn=cmd_supervise)
    sub.add_parser("devices").set_defaults(fn=cmd_devices)
    sub.add_parser("mcp").set_defaults(fn=cmd_mcp)
    a = sub.add_parser("add-audio")
    a.add_argument("file")
    a.add_argument("--source", choices=["me", "call"], default="call")
    a.set_defaults(fn=cmd_add_audio)
    b = sub.add_parser("bench", help="accuracy test bench")
    b.add_argument("action", choices=["add", "collect", "list", "run"])
    b.add_argument("file", nargs="?")
    b.add_argument("--text", default=None)
    b.add_argument("--n", type=int, default=30)
    b.add_argument("--engines", nargs="*", default=None, help="e.g. local:small local:hinglish-prime groq:whisper-large-v3")
    b.add_argument("--drafts", action="store_true", help="also score clips whose text is not corrected yet")
    b.set_defaults(fn=cmd_bench)
    for name, fn in (("process", cmd_process), ("export", cmd_export)):
        x = sub.add_parser(name)
        x.add_argument("--day", default=None)
        x.set_defaults(fn=fn)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
