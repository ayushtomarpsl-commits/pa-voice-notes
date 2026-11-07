"""Offline heuristic tagger: marks transcript lines that look like to-dos/bugs/deploys
before Claude runs. English + Hinglish (Roman) + common Devanagari forms.
Pure stdlib (re). Output is a hint for the UI, never a final to-do."""
import re
from datetime import date, timedelta

F = re.IGNORECASE | re.UNICODE

def rx(*alts):  # word-ish boundaries that also work next to Devanagari
    return re.compile(r"(?<![\w])(?:" + "|".join(alts) + r")(?![\w])", F)

PATTERNS = {
    # action / request cues  -> strongest to-do signal
    "action": rx(r"karna\s+hai", r"karni\s+hai", r"karne\s+hai", r"kar\s+dena", r"kar\s+do",
                 r"kar\s+lena", r"kar\s+dunga", r"kar\s+dungi", r"kar\s+denge", r"dekh\s+lena",
                 r"dekh\s+lo", r"check\s+kar(?:\s+lena|\s+lo|o)?", r"bhej\s+dena", r"bhej\s+do",
                 r"daal\s+dena", r"daal\s+do", r"laga\s+dena", r"update\s+kar\s+dena", r"\w+\s+(?:dena|lena)",
                 r"need\s+to", r"have\s+to", r"has\s+to", r"must", r"should", r"please",
                 r"i\s*will", r"i'?ll", r"can\s+you", r"could\s+you", r"make\s+sure",
                 r"to-?do", r"action\s+item", r"follow\s+up",
                 r"करना\s+है", r"कर\s+देना", r"कर\s+दो", r"देख\s+लेना", r"भेज\s+देना",
                 r"कर\s+दूंगा", r"कर\s+दूँगा"),
    "done": rx(r"ho\s+gaya", r"ho\s+gayi", r"kar\s+diya", r"kar\s+di", r"fix\s+ho\s+gaya",
               r"deploy\s+ho\s+gaya", r"push\s+kar\s+diya", r"done", r"completed", r"fixed",
               r"हो\s+गया", r"कर\s+दिया"),
    "bug": rx(r"bug", r"issue", r"error", r"exception", r"crash", r"fail(?:s|ed|ing)?",
              r"broken", r"not\s+working", r"nahi\s+ho\s+raha", r"nahi\s+chal\s+raha",
              r"galat\s+aa\s+raha", r"nahi\s+aa\s+raha", r"null\s+reference", r"500",
              r"बग", r"एरर", r"नहीं\s+हो\s+रहा"),
    "fix": rx(r"fix", r"fixed", r"resolve[ds]?", r"patch", r"hotfix", r"root\s+cause",
              r"फिक्स"),
    "deploy": rx(r"deploy(?:ment|ed)?", r"release", r"publish", r"qa", r"q\s+a", r"uat",
                 r"prod", r"production", r"live\s+kar", r"script\s+chala", r"migration",
                 r"डिप्लॉय"),
    "git": rx(r"commit", r"merge", r"push", r"pull\s+request", r"pr", r"branch", r"rebase",
              r"partner\.?\s*dev", r"main\s+branch", r"cherry[-\s]?pick"),
    "feature": rx(r"feature", r"enhancement", r"requirement", r"new\s+screen", r"add\s+a",
                  r"chahiye", r"hona\s+chahiye", r"field\s+add", r"column\s+add", r"report",
                  r"dashboard"),
    "urgent": rx(r"urgent", r"asap", r"jaldi", r"abhi", r"turant", r"immediately",
                 r"right\s+now", r"critical", r"blocker", r"blocking", r"customer\s+blocked",
                 r"prod\s+down", r"high\s+priority", r"जल्दी", r"अभी"),
    "question": rx(r"confirm\s+kar\w*", r"pooch(?:na|h\s+lena|\s+ke)", r"puch(?:na|h\s+lena)",
                   r"waiting\s+(?:on|for)", r"pending\s+(?:on|from)", r"decide", r"clarif\w*",
                   r"kya\s+karna\s+hai"),
}

PRIORITY_RX = re.compile(
    r"(?<![\w])(?:p|pee|pi)\s*[-\s]?(?:(0|zero)(?:\s*[-\s]\s*(1|2|3|one|two|three))?|(1|2|3|one|two|three))(?![\w])",
    F)
NUM = {"0": "0", "zero": "0", "1": "1", "one": "1", "2": "2", "two": "2", "3": "3", "three": "3"}

DUE_RX = {
    "today": rx(r"aaj", r"today", r"eod", r"end\s+of\s+(?:the\s+)?day", r"aaj\s+hi", r"आज"),
    "tomorrow": rx(r"kal\s+tak", r"kal\s+(?:kar|bhej|de|daal|deploy|subah|morning)\w*",
                   r"tomorrow", r"कल\s+तक"),
    "day_after": rx(r"parso\s+tak", r"parso", r"day\s+after\s+tomorrow", r"परसों"),
    "this_week": rx(r"is\s+week", r"this\s+week", r"week\s+end\s+tak", r"by\s+friday",
                    r"friday\s+tak"),
    "monday": rx(r"monday\s+(?:tak|ko)", r"by\s+monday", r"next\s+monday"),
}
PAST_KAL = rx(r"kal\s+(?:hua|tha|thi|the|kiya|ho\s+gaya)", r"yesterday")

def _alias_rx(words):
    """['gst tool', 'नाम'] -> one pattern; spaces inside a word list are optional."""
    parts = [r"\s*".join(re.escape(w) for w in str(a).split()) for a in words if str(a).strip()]
    return rx(*parts) if parts else None


_RULES = {"key": None}


def _rules():
    """People, personal words and my name come from the user's settings (data/config.json)."""
    from . import config
    cfg = config.load()
    people = cfg.get("people") or {}
    personal = cfg.get("personal_words") or []
    me = (cfg.get("my_name") or "").strip()
    key = repr((sorted(people.items()), personal, me))
    if _RULES["key"] != key:
        names = [me] if me and me.lower() != "me" else []
        names += list(people)
        _RULES.update(
            key=key,
            people={n: _alias_rx([n] + list(a)) for n, a in people.items()},
            personal=_alias_rx(personal),
            address=(re.compile(r"^\s*(" + "|".join(re.escape(n) for n in names) + r")[\s,:-]", F)
                     if names else None),
            me=me.lower(),
        )
    return _RULES

WEIGHTS = {"action": 3, "urgent": 1, "bug": 1, "fix": 1, "deploy": 1, "git": 1,
           "feature": 1, "question": 0.5}


def _priority(text):
    m = PRIORITY_RX.search(text)
    if not m:
        return None
    if m.group(1):
        return "P0" + (f"-{NUM[m.group(2).lower()]}" if m.group(2) else "")
    return "P" + NUM[m.group(3).lower()]


def _due(text, today: date):
    if PAST_KAL.search(text) and not DUE_RX["tomorrow"].search(text):
        return None
    if DUE_RX["today"].search(text):
        return today
    if DUE_RX["tomorrow"].search(text):
        return today + timedelta(days=1)
    if DUE_RX["day_after"].search(text):
        return today + timedelta(days=2)
    if DUE_RX["monday"].search(text):
        return today + timedelta(days=(7 - today.weekday()) or 7)
    if DUE_RX["this_week"].search(text):
        return today + timedelta(days=max(0, 4 - today.weekday()))
    return None


SELF_RX = rx(r"main\s+kar\s+dunga", r"main\s+kar\s+lunga", r"main\s+dekh\s+lunga",
             r"i\s*will", r"i'?ll", r"मैं\s+कर\s+दूंगा")
def _owner(text, rules):
    if SELF_RX.search(text):
        return "me"
    m = rules["address"].search(text) if rules["address"] else None
    if m:
        name = m.group(1).lower()
        return "me" if name == rules["me"] else name.capitalize()
    return None


def tag_line(text: str, today: date) -> dict:
    tags = [k for k, p in PATTERNS.items() if p.search(text)]
    prio = _priority(text)
    due = _due(text, today)
    rules = _rules()
    people = [n for n, p in rules["people"].items() if p and p.search(text)]
    personal = bool(rules["personal"] and rules["personal"].search(text))
    score = sum(WEIGHTS.get(t, 0) for t in tags) + (2 if prio else 0) + (1 if due else 0)
    if "done" in tags and "action" not in tags:
        score -= 2  # "ho gaya" = finished, not a new to-do
    likely_todo = ("action" in tags and score >= 4) or (prio is not None and score >= 3)
    kind = ("personal" if personal else "bug_fix" if ("bug" in tags or "fix" in tags) else
            "deploy" if "deploy" in tags else "git" if "git" in tags else
            "enhancement" if "feature" in tags else "follow_up" if likely_todo else None)
    owner = _owner(text, rules)
    return {"tags": tags, "priority": prio, "owner_hint": owner, "due": due.isoformat() if due else None,
            "people": people, "personal": personal, "kind": kind,
            "score": score, "likely_todo": likely_todo,
            "likely_done": "done" in tags and "action" not in tags}


