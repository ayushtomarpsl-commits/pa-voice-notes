# PA — AI voice notes assistant (your own "Wispr Flow" for meetings)

PA runs in the background on your Windows laptop, listens to your **microphone** and your **PC sound** (the other
side of Teams / Meet / Zoom calls), turns speech into text — **Hindi, English and Hinglish** — and turns every
conversation into **topics, notes, decisions and to-dos**. A local page shows your day, and **Claude Code** can
read everything through an MCP server and help you do the work.

- Audio stays on your laptop and is deleted right after it becomes text.
- Free to run: Groq's free API for speech + notes, with an on-laptop fallback.
- Pauses by itself outside work hours, when the screen is locked or the PC sleeps; **Ctrl+Alt+P** pauses anytime.

**Full setup guide (simple English):** [docs/SETUP-GUIDE.md](docs/SETUP-GUIDE.md) ·
online: <https://computerhub-links.vercel.app/voice-notes-guide.html> · technical design: [docs/DESIGN.md](docs/DESIGN.md)

## What you get

| Screen | What it shows |
|---|---|
| **Today** | Topics discussed, conversation timeline, Focus to-dos, live transcript with speaker names |
| **Topics** | One card per subject with a dated history of every point |
| **Board** | To do / Doing / Blocked / Done (drag and drop) |
| **People** | Every voice heard — rename once, it changes everywhere |
| **Notes · Search · Settings** | Bugs, features, decisions, questions; full-text search; all settings and API keys |

```
mic + PC sound -> keep only speech (Silero VAD) -> speech to text (Groq Whisper large-v3, or laptop faster-whisper)
               -> SQLite -> notes / topics / to-dos (Groq gpt-oss, or Claude) -> web page + Claude Code (MCP)
```

## Quick start (Windows 10/11, PowerShell)

Needs **Python 3.14** (the `py` launcher) and about 2 GB of disk. Clone into a folder, for example `C:\PA`, then:

```powershell
powershell -ExecutionPolicy Bypass -File "C:\PA\scripts\setup.ps1"
```
```powershell
powershell -ExecutionPolicy Bypass -File "C:\PA\scripts\start.ps1"
```

Open <http://127.0.0.1:8765/#settings>, paste a **free Groq key** (console.groq.com → API Keys), press
**Save** and **Test key**. Then set **Your name**, **Projects**, **Vocabulary** and **My profile**.

Optional — start at login, and connect Claude Code (`/pa-process` makes notes with no API key, `/pa-today`
plans and does today's work):
```powershell
powershell -ExecutionPolicy Bypass -File "C:\PA\scripts\install-autostart.ps1"
```
```powershell
powershell -ExecutionPolicy Bypass -File "C:\PA\scripts\register-mcp.ps1"
```

## Commands

```powershell
.\.venv\Scripts\python.exe -m pa devices        # list microphones and speakers
.\.venv\Scripts\python.exe -m pa run            # listen (tray icon)
.\.venv\Scripts\python.exe -m pa serve          # web page only
.\.venv\Scripts\python.exe -m pa add-audio FILE.wav
.\.venv\Scripts\python.exe -m pa process        # make notes now
.\.venv\Scripts\python.exe -m pa export         # today's hand-off file for Claude Code
.\.venv\Scripts\python.exe -m pa bench collect --n 30   # accuracy test on your own voice
.\.venv\Scripts\python.exe -m pytest -q tests   # 48 tests, no mic or network needed
```

## Your settings stay private

Everything personal lives in `data/` (git-ignored): the database, `config.json` (your name, people, projects,
vocabulary, personal words), `profile.md`, voice fingerprints and API keys (encrypted with Windows DPAPI).
`exports/` and `models/` are ignored too.

## Privacy and office rules

PA hears colleagues and clients. Check your company's IT / data policy and tell your team you take automatic
notes. With Groq, office audio and text go to a US cloud service; if that is not allowed, set
**Speech to text = local** and **Notes made by = claude-code** in Settings. This is not legal advice.

## Contributing

Issues and pull requests are welcome. Keep the tests green and never commit anything from `data/`.

---

Built with Claude Code by **Ayush Tomar** — follow [@computer__hub](https://www.instagram.com/computer__hub/) for
the *Plan B for Software Engineers* series. License: [MIT](LICENSE).
