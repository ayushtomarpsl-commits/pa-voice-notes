"""Daily hand-off file for Claude Code: exports/YYYY-MM-DD.md

Order: what to do (to-dos by priority, with evidence), then the knowledge Claude needs to do it
(bug explanations, feature requirements, decisions, open questions, info), then a short list of
conversations. Personal items are left out by default.
"""
from __future__ import annotations

import json
from datetime import date

from . import config, db

TYPE_TITLES = [("bug_explanation", "Bug explanations"), ("feature_requirement", "Feature requirements"),
               ("decision", "Decisions"), ("question", "Open questions"), ("info", "Good to know")]


def _bullets(items) -> str:
    return "".join(f"\n  - {i}" for i in items if i)


def _note_md(n: dict) -> str:
    b = n.get("body") or {}
    t = n["type"]
    lines = [f"### N{n['id']} · {n['title']}  _({n['project']})_"]
    if t == "bug_explanation":
        lines += [f"- **Problem:** {b.get('problem') or '-'}", f"- **Root cause:** {b.get('root_cause') or 'not said'}",
                  f"- **Fix:** {b.get('fix') or 'not said'}"]
        if b.get("modules_or_files"):
            lines.append(f"- **Where:** {', '.join(b['modules_or_files'])}")
        if b.get("how_to_verify"):
            lines.append(f"- **How to verify:** {b['how_to_verify']}")
    elif t == "feature_requirement":
        lines += [f"- **What:** {b.get('what') or '-'}", f"- **Why:** {b.get('why') or 'not said'}"]
        if b.get("acceptance_criteria"):
            lines.append("- **Acceptance criteria:**" + _bullets(b["acceptance_criteria"]))
        if b.get("screens_apis_db"):
            lines.append(f"- **Screens / APIs / DB:** {', '.join(b['screens_apis_db'])}")
    elif t == "decision":
        lines += [f"- **By:** {b.get('by_whom') or 'not said'}", f"- **Why:** {b.get('reason') or 'not said'}"]
    elif t == "question":
        lines.append(f"- **Waiting on:** {b.get('waiting_on') or 'unknown'}")
    elif t == "info":
        # the AI sometimes puts the content in another field (what / why / problem ...): show what exists
        extra = [str(b[k]) for k in ("text", "what", "why", "problem", "fix", "decision", "question", "reason")
                 if b.get(k) and not isinstance(b[k], list)]
        lines.append(" ".join(extra) or "")
    if n.get("evidence"):
        lines.append(f"- _Heard:_ “{n['evidence']}”")
    return "\n".join(lines)


def build(day: str | None = None, include_personal: bool | None = None) -> str:
    day = day or date.today().isoformat()
    cfg = config.load()
    if include_personal is None:
        include_personal = not cfg.get("exclude_personal_from_handoff", True)

    def keep(item):
        return include_personal or item.get("project") != "Personal"

    todos = [t for t in db.list_todos(status="open") if keep(t)]
    mine = [t for t in todos if (t["owner"] or "me").lower() == "me"]
    others = [t for t in todos if (t["owner"] or "me").lower() != "me"]
    done_today = [t for t in db.list_todos(status="done") if keep(t) and (t["done_at"] or "")[:10] == day]
    notes = [n for n in db.list_notes(day=day) if keep(n)]
    convs = [c for c in db.list_conversations(day=day) if keep(c) and c["status"] != "skipped"]  # no personal/small talk

    out = [f"# Work hand-off — {day}",
           "",
           "> Made by PA from what I heard today. Text comes from speech recognition, so names and words",
           "> can be slightly wrong — check quotes before acting. IDs: T = to-do, N = note.",
           "> When you finish a to-do, mark it done with the `pa-notes` MCP tool `update_todo`.",
           ""]
    out.append(f"## My to-dos ({len(mine)} open)")
    if not mine:
        out.append("_Nothing open._")
    for t in mine:
        flags = []
        if t["due"]:
            flags.append(f"due {t['due']}" + (" ⚠ overdue" if t["due"] < day else ""))
        if t["day"] < day:
            flags.append(f"carried from {t['day']}")
        if t["confidence"] < 0.6:
            flags.append("unsure — check")
        head = f"- [ ] **T{t['id']} [{t['priority']}] {t['title']}** — {t['project']} · {t['kind']}"
        out.append(head + (f" · {', '.join(flags)}" if flags else ""))
        if t["detail"]:
            out.append(f"  - {t['detail']}")
        if t["evidence"]:
            out.append(f"  - _Heard:_ “{t['evidence']}”")
    if others:
        out += ["", f"## Waiting on others ({len(others)})"]
        out += [f"- T{t['id']} **{t['owner']}**: {t['title']} [{t['priority']}]" + (f" due {t['due']}" if t["due"] else "")
                for t in others]
    if done_today:
        out += ["", f"## Done today ({len(done_today)})"] + [f"- [x] T{t['id']} {t['title']}" for t in done_today]

    touched = [dict(r) for r in db.conn().execute(
        "SELECT DISTINCT t.id, t.title, t.project, t.first_day FROM topic_updates u JOIN topics t ON t.id=u.topic_id "
        "WHERE u.day=? ORDER BY t.updated_at DESC", (day,))]
    touched = [t for t in touched if include_personal or t["project"] != "Personal"]
    if touched:
        out += ["", f"## Topics discussed today ({len(touched)})",
                "> Every point said today, per topic. Older days are in the PA page (Topics) or MCP `get_topic`."]
        for t in touched:
            since = f" · discussed since {t['first_day']}" if t["first_day"] < day else ""
            out.append(f"### TOPIC {t['id']} · {t['title']}  _({t['project']}{since})_")
            for u in db.conn().execute("SELECT summary, points FROM topic_updates WHERE topic_id=? AND day=? ORDER BY id",
                                       (t["id"], day)):
                if u["summary"]:
                    out.append(f"**Now:** {u['summary']}")
                for pt in json.loads(u["points"] or "[]"):
                    out.append(f"- {pt}")
            out.append("")

    for typ, label in TYPE_TITLES:
        group = [n for n in notes if n["type"] == typ]
        if group:
            out += ["", f"## {label}"] + [_note_md(n) + "\n" for n in group]

    if convs:
        out += ["", "## Conversations today"]
        for c in convs:
            title = c["title"] or "(not processed yet)"
            out.append(f"- {c['started_at'][11:16]}–{c['ended_at'][11:16]} **{title}**"
                       + (f" — {c['summary']}" if c["summary"] else "") + f"  `conversation {c['id']}`")
    unprocessed = [c for c in convs if c["status"] in ("open", "ended", "error")]
    if unprocessed:
        out += ["", f"> {len(unprocessed)} conversation(s) are not turned into notes yet. "
                    "In Claude Code run the `pa-process` skill, or click Process in the PA page."]
    return "\n".join(out) + "\n"


def write(day: str | None = None) -> str:
    day = day or date.today().isoformat()
    config.EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = config.EXPORT_DIR / f"{day}.md"
    path.write_text(build(day), encoding="utf-8")
    db.log_event("export", str(path))
    return str(path)
