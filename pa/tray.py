"""System-tray icon: red dot = listening, grey = paused, amber = problem.
Menu: open My Day, pause/resume, private mode, process now, quit."""
from __future__ import annotations

import threading
import time
import webbrowser

import pystray
from PIL import Image, ImageDraw

COLORS = {"listening": (217, 72, 15), "paused": (140, 140, 140), "error": (230, 160, 0)}


def _icon(color) -> Image.Image:
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((6, 6, 58, 58), fill=color + (255,))
    d.ellipse((22, 22, 42, 42), fill=(255, 255, 255, 230))
    return img


def run_tray(assistant, url: str, on_quit) -> None:
    """Blocks until Quit. Call from the main thread."""

    def mood() -> str:
        s = assistant.status()
        if any(x["state"] == "error" for x in s["sources"]):
            return "error"
        return "listening" if s["listening"] else "paused"

    def refresh(icon=None):
        icon = icon or tray
        m = mood()
        icon.icon = _icon(COLORS[m])
        reason = assistant.pause_reason()
        icon.title = "PA — listening" if m == "listening" else f"PA — {reason or 'check devices'}"

    tray = pystray.Icon(
        "PA", _icon(COLORS["paused"]), "PA",
        menu=pystray.Menu(
            pystray.MenuItem("Open My Day", lambda: webbrowser.open(url), default=True),
            pystray.MenuItem(lambda _: "Resume listening" if assistant.manual_pause else "Pause listening",
                             lambda: (assistant.toggle(), refresh())),
            pystray.MenuItem("Private mode", lambda: (assistant.set_private(not assistant.private), refresh()),
                             checked=lambda _: assistant.private),
            pystray.MenuItem("Make notes now", lambda: assistant.process_now()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit PA", lambda: (on_quit(), tray.stop())),
        ))
    def setup(icon):
        icon.visible = True
        refresh(icon)

        def loop():  # catch changes that do not call on_change (device errors, work-hours switch)
            while icon.visible:
                time.sleep(5)
                try:
                    refresh(icon)
                except Exception:
                    pass
        threading.Thread(target=loop, daemon=True, name="tray-refresh").start()

    assistant.on_change = lambda: refresh()
    tray.run(setup=setup)
