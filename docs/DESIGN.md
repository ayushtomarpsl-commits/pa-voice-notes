# PA — Design (as built, 2026-10-15)

Short technical reference. For the simple-English guide see `BUILD-STEPS.md`.

## Data flow

```
AudioSource('me')   mic (WASAPI, default or name hint) ─┐  int16 → float32 mono → soxr → 16 kHz
AudioSource('call') default speaker WASAPI loopback ────┤
                                                        ▼
            Segmenter (Silero VAD v6 onnx from faster_whisper/assets, 512-sample frames,
            on 0.5 / off 0.35, pre-roll 300 ms, end after 900 ms silence, max 28 s, min 600 ms)
                                                        ▼
            data/queue/<start>_<end>_<source>.wav   (crash-safe hand-off)
                                                        ▼
            Transcriber thread: pick_group() packs same-source pieces into one ≤28 s window
            → faster-whisper small int8, 8 threads, language 'en', beam 1, vocabulary prompt
            → junk filter → echo dedupe (mic text ≈ call text in same window) → offline tagger hint
            → db.add_segment() (joins/creates conversation; gap > 4 min = new) → wav deleted
                                                        ▼
            scheduler (30 s): work-hours/lunch auto-pause, close idle conversations ('ended'),
            auto-process with Claude API when a key exists and the queue is empty
                                                        ▼
            extract.call_claude → import_extraction (shared with MCP save_extraction)
                                                        ▼
            SQLite (WAL, FTS5)  ←→  FastAPI web UI 127.0.0.1:8765  ←→  MCP 'pa-notes' (stdio)  →  exports/<day>.md
```

## Threads (one process, `python -m pa run`)
audio-me, audio-call, device-watch (soundcard default-name poll, 4 s), transcriber, scheduler,
win-events (hidden window: Ctrl+Alt+P hotkey, suspend/resume, lock/unlock), web (uvicorn), main = tray.
PortAudio is not thread-safe: all PyAudio init/open/close/terminate go through `audio._PA_LOCK`, and a
device change makes every source close before re-init so PortAudio rescans devices.

## Pause = any of
manual pause (button / tray / hotkey), private mode, sleep, screen locked, outside work hours, lunch.
Paused → streams closed (Windows mic indicator off), open utterance flushed to queue.

## Storage (`data/`)
`pa.sqlite` tables: conversations, segments, notes, todos, events, processing_runs + FTS5
(`unicode61` with `M*` categories so Devanagari words stay whole). `config.json` (defaults in
`pa/config.py`), `profile.md` (user-editable profile injected into the prompt), `api_key.dpapi`
(DPAPI-encrypted, current Windows user), `ui_token`, `queue/`, `audio/` (only if keep_audio_days > 0), `logs/`.

## Claude call
`client.beta.messages.create(model=claude-opus-5-5, max_tokens=16000, system=[prompt, cache_control
ephemeral], output_config={effort: medium, format: json_schema}, betas=[server-side-fallback-2026-07-01],
fallbacks="default")`. Schema in `pa/schema.py` (all objects additionalProperties:false, all required).
Import: quotes checked against transcript (fuzzy) → confidence ×0.6 if missing; `same_as` / title
similarity > 0.85 updates the existing to-do instead of adding; re-processing replaces untouched auto items.

## Web API (all JSON; writes need header `X-PA-Token`; Host must be 127.0.0.1/localhost)
GET status, devices, days, conversations?day, segments?day&conversation_id&after_id, live, notes?day&type&project,
todos?status(today|open|done|dropped|all)&project&priority, search?q, export/{day} (markdown), settings ·
POST pause{paused}, private{on}, process{day}, conversations/{id}/process, notes, todos, export/{day} ·
PATCH conversations/{id}, notes/{id}, todos/{id}{…, comment} · PUT settings{config, profile}, apikey{key} ·
DELETE segments/{id}, notes/{id}, todos/{id}.

## MCP tools (`pa-notes`, mcp 2.3 `MCPServer`)
list_days, get_day, list_todos, update_todo, add_todo, add_note, search, get_transcript,
get_extraction_guide, get_unprocessed, save_extraction.

## Tests
`tests/` — 17 tests, no mic or network needed: DB, conversation grouping, to-do ordering/log, FTS,
Hinglish tagger, extraction import (dedupe, quote check, re-process), fake Claude call shape, schema
safety, hand-off excludes Personal, VAD on a SAPI-generated WAV, junk filter, DPAPI round-trip,
web API security + flows, MCP over real stdio.
Run (from the project folder): `.\.venv\Scripts\python.exe -m pytest -q tests`

## Measured (2026-10-15, on battery 1.7 GHz)
small int8 8 threads: 72 s English → 25 s (2.9x real time) with packing; per-piece + auto language was 0.6x.
base: ~3x faster than small but more errors ("Dial Pipeline"). English accuracy with small: near perfect on test.

## Free setup additions (2026-10-16)
- `pa/groq_api.py` — httpx client: `/audio/transcriptions` (verbose_json, language, prompt) and
  `/chat/completions` (json_schema strict → json_object fallback). 429 → `RateLimited(retry_after)`.
- `pa/transcribe.py` — `Engine` = Groq (if `stt_engine=groq`, key, not cooling down) else `LocalWhisper`;
  `transcribe_segments(audio, engine="groq:<m>"|"local:<m>")` used by the bench. `LOCAL_MODELS` maps
  `hinglish-prime` / `hinglish-swift`.
- `pa/extract.py` — `call_llm` dispatch (groq | claude | claude-code), pre-filter by word count,
  `import_extraction(..., cfg, force)` applies office-only (`category`), flat-note bodies drop empty fields.
- `pa/keys.py` — DPAPI secret store. `pa/bench.py` — clips, WER, key-word recall, HTML report.
- API: `PUT /api/apikey {name, key}`, `POST /api/check/groq`, `POST /api/conversations/{id}/process?force=true`.
- Schema v2: `conversations.category`; status `skipped` for non-office talk.

## v3 (2026-10-16 night): voices, topics with history, complete points, new UI
- **Voices** (`pa/speakers.py`): sherpa-onnx `SpeakerEmbeddingExtractor` with 3D-Speaker CAM++ zh_en "advanced" (multilingual; chosen 16-Oct: 100% on real Hindi speech-only halves, 99% on 2 s pieces; English-only CAM++ 83-96%, ResNet34 65-88%;
  `models/speaker/`). Per Whisper segment (>= 1.2 s) a 512-d fingerprint; cosine vs speaker centroids;
  match >= `speaker_threshold` (0.50), previous speaker gets +0.08; a new person only from >= 2.5 s;
  new voices get a placeholder from `NAME_POOL` (kind `auto`). Centroid = running mean (updated only on
  sure matches). `maybe_enroll_me`: dominant mic voice (>= 60% of >= 120 s) becomes `my_name` (kind `me`).
  `rename` / `merge` rewrite the name in todos, notes, conversations, topics, topic_updates.
  Transcriber splits each piece into runs of the same speaker and saves one line per run.
  Measured: real speech-only halves of 1-3.5 s overlap (same 0.73 mean vs different 0.31 mean), so short
  turns can be mis-assigned; longer pieces and centroids help; the People page fixes mistakes.
- **Topics** (tables `topics`, `topic_updates`): each conversation's answer has `topics[{title, same_as,
  summary, points[]}]`; `save_topics` continues a topic (same_as id, same title, or title similarity >= 0.75)
  or creates one, then appends a dated section. Old sections are never edited. Notes/todos carry `topic`.
  `merge_duplicate_topics` (one small Groq call) merges same-subject topics after each batch.
  `conversations.points` = every point, with its topic.
- **Groq budget**: free plan = 8,000 tokens/minute per model, ~200k tokens/day; models rotate
  gpt-oss-120b -> qwen/qwen3.8-27b -> gpt-oss-20b (qwen tested better than 20b for English points).
  Prompt (short, no example) ~3.1k tokens + context <= 25 todos / 20 topics + transcript part 1,500 +
  answer <= 1,800 tokens.
- **Board**: `todos.stage` (todo / doing / blocked) + status done; drag and drop.
- **UI** (`pa/static`): Geist fonts bundled offline; zinc + one emerald accent; pages Today / Topics /
  Board / People / Notes / Search / Settings; drawers `#topics/<id>`, `#conv/<id>`, `#todo/<id>`.
- **API** additions: `/api/topics`, `/api/topics/{id}` (+PATCH, /merge), `/api/people` (+PATCH name/me,
  /merge), `/api/overview`, `/api/conversations/{id}`; search returns topics. MCP: `list_topics`,
  `get_topic`, `list_people`; hand-off has "Topics discussed today".
- Schema v3 migration is additive (backup `data/pa-backup-before-v3.sqlite`).
