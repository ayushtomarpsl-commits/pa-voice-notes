"""JSON schema for one conversation -> notes + to-dos (Claude structured output).

API-safe: every object has additionalProperties:false and every property is required;
no minimum/maximum/minLength. The project list comes from the settings, so you can rename
projects without touching code.
"""
PRIORITIES = ["P0", "P0-1", "P0-2", "P0-3", "P1", "P2", "P3", "none"]
TODO_KINDS = ["bug_fix", "enhancement", "deploy", "git", "test_verify", "follow_up",
              "decision_pending", "learning", "personal", "other"]

S = {"type": "string"}
S_NULL = {"type": ["string", "null"]}
S_LIST = {"type": "array", "items": S}


def obj(props: dict) -> dict:
    return {"type": "object", "properties": props,
            "required": list(props.keys()), "additionalProperties": False}


def build_schema(projects: list[str]) -> dict:
    projects = list(dict.fromkeys([*projects, "Personal", "Other"]))
    project = {"type": "string", "enum": projects}

    def note(kind: str, fields: dict) -> dict:
        return obj({"type": {"type": "string", "const": kind}, "project": project, "topic": S,
                    **fields, "evidence_quote": S})

    notes = [
        note("bug_explanation", {"title": S, "problem": S, "root_cause": S_NULL, "fix": S_NULL,
                                 "modules_or_files": S_LIST, "how_to_verify": S_NULL}),
        note("feature_requirement", {"title": S, "what": S, "why": S_NULL,
                                     "acceptance_criteria": S_LIST, "screens_apis_db": S_LIST}),
        note("decision", {"decision": S, "by_whom": S_NULL, "reason": S_NULL}),
        note("info", {"title": S, "text": S}),
        note("question", {"question": S, "waiting_on": S_NULL}),
    ]
    todo = obj({
        "title": S,
        "detail": S,
        "kind": {"type": "string", "enum": TODO_KINDS},
        "priority": {"type": "string", "enum": PRIORITIES},
        "owner": S,                        # "me" or a person's name
        "due": S_NULL,                     # YYYY-MM-DD or null
        "project": project,
        "topic": S,                        # exact title of one of the answer's topics
        "status": {"type": "string", "enum": ["open", "done"]},
        "same_as": S_NULL,                 # id of an existing open to-do (from context) or null
        "evidence_quote": S,
        "confidence": {"type": "number"},  # 0..1, clamped when saved
    })
    return obj({
        "conversation": obj({"title": S, "summary": S, "people": S_LIST, "project": project,
                             "category": CATEGORY}),
        "topics": {"type": "array", "items": TOPIC},
        "notes": {"type": "array", "items": {"anyOf": notes}},
        "todos": {"type": "array", "items": todo},
    })


CATEGORY = {"type": "string", "enum": ["work", "personal", "small_talk"]}
# one subject of the talk; points keep EVERY detail that was said about it, in order
TOPIC = obj({"title": S, "same_as": S_NULL, "summary": S, "points": S_LIST})
NOTE_TYPES = ["bug_explanation", "feature_requirement", "decision", "info", "question"]


def build_flat_schema(projects: list[str]) -> dict:
    """Same content as build_schema, but each note is ONE flat object (no anyOf), because the
    Groq strict mode works best with simple schemas. Fields that do not belong to a note's type
    are null or []."""
    full = build_schema(projects)
    project = full["properties"]["conversation"]["properties"]["project"]
    note = obj({
        "type": {"type": "string", "enum": NOTE_TYPES}, "project": project, "topic": S, "title": S,
        "problem": S_NULL, "root_cause": S_NULL, "fix": S_NULL, "modules_or_files": S_LIST, "how_to_verify": S_NULL,
        "what": S_NULL, "why": S_NULL, "acceptance_criteria": S_LIST, "screens_apis_db": S_LIST,
        "decision": S_NULL, "by_whom": S_NULL, "reason": S_NULL, "text": S_NULL,
        "question": S_NULL, "waiting_on": S_NULL, "evidence_quote": S,
    })
    return obj({
        "conversation": full["properties"]["conversation"],
        "topics": full["properties"]["topics"],
        "notes": {"type": "array", "items": note},
        "todos": full["properties"]["todos"],
    })
