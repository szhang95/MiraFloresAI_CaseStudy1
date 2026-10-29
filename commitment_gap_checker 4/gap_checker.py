#!/usr/bin/env python3
"""Commitment Gap Checker

Reads a meeting transcript, finds the commitments people made, and reports
which ones are NOT yet actionable: no named owner, no real due date, vague
wording, impossible dates, or a large item nobody claimed.

Design (see README):
  1. EXTRACT   - decide which sentences are commitments and who/when they name.
                 Default: offline rule-based extractor. Optional: an LLM (--llm).
  2. VALIDATE  - deterministic code: resolve "Friday"/"tomorrow" against the
                 meeting date, reject impossible dates, count and sort gaps.
                 The LLM never decides whether a date is valid.

Standard library only. Python 3.9+.
"""
from __future__ import annotations

import argparse
import calendar
import json
import os
import re
import sys
import textwrap
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

__version__ = "0.1.0"


class InputError(Exception):
    """The input cannot be analysed (empty, garbled, wrong language, bad date)."""


# --------------------------------------------------------------------------
# Data structures
# --------------------------------------------------------------------------
@dataclass
class Utterance:
    line_no: int
    speaker: Optional[str]
    side: str  # "client" | "internal" | "unknown"
    text: str


@dataclass
class Candidate:
    """What an extractor (rules or LLM) claims it found. Not yet validated."""
    line_no: int
    speaker: Optional[str]
    sentence: str
    owner_raw: Optional[str]
    due_raw: Optional[str]
    hedged: bool
    owner_inferred: bool = False
    notes: list = field(default_factory=list)


@dataclass
class Commitment:
    id: int
    line: int
    speaker: Optional[str]
    sentence: str
    owner: Optional[str]
    owner_side: Optional[str]
    due_text: Optional[str]
    due_date: Optional[str]  # ISO date, only when resolved and valid
    status: str
    issues: list
    notes: list
    fixes: list


# --------------------------------------------------------------------------
# Constants (heuristics you may want to tune; each is an explicit assumption)
# --------------------------------------------------------------------------
_MON = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|"
        r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)")
_DAYS = r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
MONTH_NUM = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
DAY_IDX = {d: i for i, d in enumerate(
    ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"])}
NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
             "seven": 7, "ten": 10}

# (kind, regex). Earlier = more precise. A precise date beats a vague phrase
# when a sentence contains both ("by Friday, ASAP" -> Friday).
DUE_PATTERNS = [
    ("iso", r"\b\d{4}-\d{1,2}-\d{1,2}\b"),
    ("mdy", rf"\b{_MON}\.?\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,?\s+\d{{4}})?\b"),
    ("dmy", rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?{_MON}\b(?:,?\s+\d{{4}})?"),
    ("slash", r"\b(?!24/7\b)\d{1,2}/\d{1,2}(?:/\d{2,4})?\b"),
    ("end_of", r"\bend of (?:the |this )?(?:day|week|month)\b|\b(?:eod|eow|eom)\b"),
    ("relday", r"\b(?:today|tomorrow|tonight)\b"),
    ("in_n", r"\bin\s+(?:\d+|one|two|three|four|five|six|seven|ten)\s+(?:business\s+)?(?:days?|weeks?)\b"),
    ("weekday", rf"\b(?:(?:next|this)\s+)?{_DAYS}\b"),
    ("vague", r"\b(?:asap|as soon as (?:possible|we can)|soon|shortly|sometime|at some point|"
              r"later|whenever|when (?:possible|we can)|in the near future|next week|"
              r"next month|this week|next quarter)\b"),
]
DUE_RES = [(k, re.compile(p, re.I)) for k, p in DUE_PATTERNS]

HEDGE_RE = re.compile(
    r"\b(maybe|might|perhaps|possibly|probably|hopefully|i guess|sort of|kind of|"
    r"try to|try and|see if|look into|looking into|take a look|have a look|"
    r"think about|explore|at some point)\b", re.I)

# Words suggesting a large piece of work. Used ONLY to escalate an item that
# already has no owner. Assumption: such items are the ones people avoid.
HARD_RE = re.compile(
    r"integrat|\bsync|automat|migrat|\bapi\b|\bcrm\b|real-time|reconcil|dashboard|"
    r"webhook|pipeline|rebuild|redesign|across (?:all|every|each)", re.I)

GENERIC_OWNERS = {"we", "us", "our team", "the team", "team", "someone", "somebody",
                  "anyone", "anybody", "everyone", "nobody", "they", "you", "all"}

NOT_NAMES = {"that", "this", "it", "there", "what", "which", "who", "they", "he", "she",
             "we", "you", "i", "then", "also", "yes", "no", "and", "but", "so", "well",
             "okay", "ok", "thanks", "right", "sure", "great", "today", "tomorrow",
             "everything", "everyone", "nothing", "something", "someone", "january",
             "february", "march", "april", "may", "june", "july", "august", "september",
             "october", "november", "december", "monday", "tuesday", "wednesday",
             "thursday", "friday", "saturday", "sunday"}

ISSUE_LABEL = {
    "MISSING_OWNER": "missing owner",
    "MISSING_DATE": "missing date",
    "VAGUE_ACTION": "vague wording",
    "BAD_DATE": "date problem",
    "UNCLAIMED_HARD": "unclaimed large item",
}
FIX_TEXT = {
    "MISSING_OWNER": "Name one person who owns this.",
    "MISSING_DATE": "Agree a specific calendar due date (not 'soon' / 'next week').",
    "VAGUE_ACTION": "Say what 'done' looks like (a deliverable, not 'look into' / 'try').",
    "BAD_DATE": "Re-confirm the date with the people involved.",
    "UNCLAIMED_HARD": "Large item with no owner: assign a lead, or split it into smaller pieces, before the meeting ends.",
}
# Which issue becomes the headline status when several apply.
STATUS_PRIORITY = ["BAD_DATE", "UNCLAIMED_HARD", "MISSING_OWNER", "MISSING_DATE", "VAGUE_ACTION"]
STATUS_NAME = {"BAD_DATE": "bad_date", "UNCLAIMED_HARD": "unclaimed_hard",
               "MISSING_OWNER": "missing_owner", "MISSING_DATE": "missing_date",
               "VAGUE_ACTION": "vague"}


# --------------------------------------------------------------------------
# Input validation and parsing
# --------------------------------------------------------------------------
_COMMON_WORDS = set("""the to a an and or of in on for with that this is are was be will i we you it
at by from as can have has not but if so do does what when who how they he she our your my
please yes no thanks send share need should would could about up out all""".split())

HEADER_DATE_RE = re.compile(r"^\s*meeting date\s*:\s*(\S.*?)\s*$", re.I)
HEADER_TITLE_RE = re.compile(r"^\s*meeting\s*:\s*(\S.*?)\s*$", re.I)
SPEAKER_RE = re.compile(
    r"^\s*(?P<name>[A-Za-z][A-Za-z.'\- ]{0,30}?)\s*(?:\((?P<side>[^)]{1,30})\))?\s*:\s*(?P<text>\S.*)$")
SILENCE_RE = re.compile(r"^\s*\[(?:silence|no response|no one (?:responds?|answers?)|nobody (?:responds?|answers?))[^\]]*\]\s*$", re.I)


def validate_text(text: str) -> None:
    """Reject input that cannot be a transcript. Raises InputError."""
    if not text or not text.strip():
        raise InputError("the input is empty. Provide a meeting transcript.")
    non_space = [c for c in text if not c.isspace()]
    cjk = sum(1 for c in non_space if "\u4e00" <= c <= "\u9fff"
              or "\u3040" <= c <= "\u30ff" or "\uac00" <= c <= "\ud7af")
    if cjk / len(non_space) > 0.3:
        raise InputError("the transcript is mostly non-English text. The offline rule-based "
                         "extractor supports English only. Use --llm, or translate first.")
    letters = sum(1 for c in non_space if c.isalpha())
    if letters / len(non_space) < 0.6:
        raise InputError("the input does not look like a transcript "
                         f"(only {letters * 100 // len(non_space)}% of characters are letters). "
                         "It may be garbled or a binary/encoded file.")
    tokens = re.findall(r"[A-Za-z']+", text.lower())
    if len(tokens) >= 8:
        common = sum(1 for t in tokens if t in _COMMON_WORDS) / len(tokens)
        if common < 0.08:
            raise InputError("the input does not look like English sentences "
                             "(almost no common words found). It may be garbled.")


def parse_header(text: str) -> tuple:
    """Return (title or None, meeting date string or None) from optional header lines."""
    title = mdate = None
    for line in text.splitlines():
        m = HEADER_DATE_RE.match(line)
        if m and mdate is None:
            mdate = m.group(1)
            continue
        m = HEADER_TITLE_RE.match(line)
        if m and title is None:
            title = m.group(1)
    return title, mdate


def parse_date_arg(s: str) -> date:
    try:
        return datetime.strptime(s.strip(), "%Y-%m-%d").date()
    except ValueError:
        raise InputError(f"meeting date '{s}' is not a valid YYYY-MM-DD date.")


def side_of(label: Optional[str]) -> str:
    if not label:
        return "unknown"
    low = label.lower()
    if "client" in low or "customer" in low or "external" in low:
        return "client"
    return "internal"


def parse_transcript(text: str):
    """Return (utterances, silence_after) where silence_after is a set of line numbers
    of utterances immediately followed by a [silence]/[no response] marker."""
    utterances: list = []
    silence_after: set = set()
    for i, raw in enumerate(text.splitlines(), 1):
        if (not raw.strip() or raw.lstrip().startswith("#")
                or HEADER_DATE_RE.match(raw) or HEADER_TITLE_RE.match(raw)):
            continue
        if SILENCE_RE.match(raw):
            if utterances:
                silence_after.add(utterances[-1].line_no)
            continue
        m = SPEAKER_RE.match(raw)
        if m:
            utterances.append(Utterance(i, m.group("name").strip(), side_of(m.group("side")),
                                        m.group("text").strip()))
        else:
            utterances.append(Utterance(i, None, "unknown", raw.strip()))
    return utterances, silence_after


def build_attendees(utterances) -> dict:
    """lowercase full name / first name -> (display name, side)."""
    out = {}
    for u in utterances:
        if u.speaker:
            out[u.speaker.lower()] = (u.speaker, u.side)
            out.setdefault(u.speaker.lower().split()[0], (u.speaker, u.side))
    return out


def split_sentences(text: str) -> list:
    # protect "Oct. 15" style abbreviations from being split
    protected = re.sub(rf"\b({_MON})\.(\s+\d)", r"\1<DOT>\2", text, flags=re.I)
    parts = re.split(r"(?<=[.!?])\s+", protected.strip())
    return [p.replace("<DOT>", ".").strip() for p in parts if p.strip()]


# --------------------------------------------------------------------------
# Dates (deterministic)
# --------------------------------------------------------------------------
@dataclass
class Due:
    raw: str
    resolved: Optional[date]
    problem: Optional[str]  # None | "imprecise" | "invalid" | "before_meeting"
    note: str


def find_due_phrase(sentence: str):
    """Return (kind, raw_phrase) for the most precise time expression, or (None, None)."""
    for kind, rx in DUE_RES:
        m = rx.search(sentence)
        if m:
            return kind, m.group(0).strip()
    return None, None


def classify_due(raw: str):
    kind, phrase = find_due_phrase(raw)
    return (kind, phrase) if kind else ("unparsed", raw)


def _ymd(y: Optional[int], m: int, d: int, meeting: date) -> date:
    if y is not None:
        return date(y, m, d)  # ValueError if not a real date
    cand = date(meeting.year, m, d)
    # A year-less date more than ~6 months in the past is assumed to mean next year.
    if (meeting - cand).days > 180:
        cand = date(meeting.year + 1, m, d)
    return cand


def _add_business_days(d: date, n: int) -> date:
    while n > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def _resolve_specific(kind: str, raw: str, meeting: date) -> date:
    low = raw.lower()
    if kind == "iso":
        y, m, d = (int(x) for x in low.split("-"))
        return date(y, m, d)
    if kind in ("mdy", "dmy"):
        pat = (rf"({_MON})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(\d{{4}}))?" if kind == "mdy"
               else rf"(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({_MON})\b(?:,?\s+(\d{{4}}))?")
        mt = re.search(pat, low, re.I)
        a, b, yr = mt.group(1), mt.group(2), mt.group(3)
        month_s, day_s = (a, b) if kind == "mdy" else (b, a)
        return _ymd(int(yr) if yr else None, MONTH_NUM[month_s[:3]], int(day_s), meeting)
    if kind == "slash":
        parts = [int(p) for p in low.split("/")]  # US convention: month/day[/year]
        yr = None
        if len(parts) == 3:
            yr = parts[2] + 2000 if parts[2] < 100 else parts[2]
        return _ymd(yr, parts[0], parts[1], meeting)
    if kind == "end_of":
        if "day" in low or low == "eod":
            return meeting
        if "week" in low or low == "eow":
            return meeting + timedelta((4 - meeting.weekday()) % 7)  # that Friday
        return date(meeting.year, meeting.month, calendar.monthrange(meeting.year, meeting.month)[1])
    if kind == "relday":
        return meeting + timedelta(days=1) if "tomorrow" in low else meeting
    if kind == "in_n":
        mt = re.search(r"in\s+(\d+|\w+)\s+(business\s+)?(day|week)", low)
        n = int(mt.group(1)) if mt.group(1).isdigit() else NUM_WORDS[mt.group(1)]
        if mt.group(3) == "week":
            return meeting + timedelta(weeks=n)
        return _add_business_days(meeting, n) if mt.group(2) else meeting + timedelta(days=n)
    if kind == "weekday":
        mt = re.search(rf"(next|this)?\s*({_DAYS})", low)
        mod, idx = mt.group(1), DAY_IDX[mt.group(2)]
        if mod == "next":  # that weekday in the following calendar week
            return meeting + timedelta(days=7 - meeting.weekday()) + timedelta(days=idx)
        if mod == "this":
            return meeting + timedelta((idx - meeting.weekday()) % 7)
        return meeting + timedelta((idx - meeting.weekday()) % 7 or 7)  # next occurrence, never today
    raise ValueError(kind)


def resolve_due(raw: str, meeting: date) -> Due:
    kind, phrase = classify_due(raw)
    if kind in ("vague", "unparsed"):
        return Due(phrase, None, "imprecise", f"'{phrase}' is not a specific date")
    try:
        d = _resolve_specific(kind, phrase, meeting)
    except ValueError:
        return Due(phrase, None, "invalid", f"'{phrase}' is not a real calendar date")
    if d < meeting:
        return Due(phrase, d, "before_meeting",
                   f"'{phrase}' resolves to {d.isoformat()}, which is before the meeting date {meeting.isoformat()}")
    return Due(phrase, d, None, f"{d.strftime('%a')} {d.isoformat()}")


# --------------------------------------------------------------------------
# Extraction (rule-based, offline)
# --------------------------------------------------------------------------
_ASK_ANY = re.compile(r"\b(?:can|could|will|would|should)\s+(?P<who>someone|anyone|somebody|anybody)\s+(?P<action>.+)", re.I)
_REQUEST = re.compile(r"^\s*(?:(?P<name>[A-Z][a-z]+),?\s+)?(?i:can|could|would|will) you(?i: please)?\s+(?P<action>.+)")
_PLEASE = re.compile(r"^\s*(?:(?P<name>[A-Z][a-z]+),\s+)?(?i:please)\s+(?P<action>.+)")
_SELF = re.compile(r"\b(?:i'll|i will|i'm going to|i am going to|let me)\s+(?P<action>.+)", re.I)
_SELF_WEAK = re.compile(r"\bi can\s+(?P<action>.+)", re.I)
_TEAM = re.compile(r"\b(?:we'll|we will|we're going to)\s+(?P<action>.+)", re.I)
_LETS = re.compile(r"\blet's\s+(?P<action>.+)", re.I)
_OBLIG = re.compile(r"\b(?P<who>we|someone|somebody|anyone|the team|our team)\s+(?:should|need to|needs to|have to|has to|must|ought to|will need to)\s+(?P<action>.+)", re.I)
_WOULD_BE = re.compile(r"\bit would be (?:good|great|nice|helpful|useful) (?:to|if we) (?P<action>.+)", re.I)
_NAME_WILL = re.compile(r"\b(?P<name>[A-Z][a-z]+)\s+(?i:will|is going to|'ll)\s+(?P<action>.+)")


def _other_side(side: str) -> Optional[str]:
    return {"client": "internal", "internal": "client"}.get(side)


def classify_sentence(sentence: str, speaker, side, attendees):
    """Return (owner_raw, inferred, note) if the sentence is a commitment, else None."""
    s = sentence.strip()
    is_question = s.endswith("?")
    due_kind, _ = find_due_phrase(s)

    if _ASK_ANY.search(s):
        return None, False, "asked of 'someone' - nobody named"
    m = _REQUEST.match(s)
    if m:
        if m.group("name"):
            return m.group("name"), False, None
        other = _other_side(side)
        people = sorted({v[0] for v in attendees.values() if other and v[1] == other})
        if len(people) == 1:
            return people[0], True, f"owner inferred: {people[0]} is the only {other} participant - confirm"
        return None, False, "request made to the room without naming who"
    if is_question:
        return "__none__"
    m = _PLEASE.match(s)
    if m:
        return (m.group("name"), False, None) if m.group("name") else (None, False, "request without a named owner")
    if _SELF.search(s):
        return speaker, False, None if speaker else "speaker unknown"
    if _SELF_WEAK.search(s) and due_kind:  # "I can" only counts when a time is attached
        return speaker, False, None if speaker else "speaker unknown"
    # "Let's ..." is usually a pleasantry ("Let's get started"), so it only counts with a time attached.
    if _TEAM.search(s) or (_LETS.search(s) and due_kind):
        return "we", False, "assigned to 'we' (a group), not a person"
    m = _OBLIG.search(s)
    if m:
        return m.group("who").lower(), False, f"stated as an obligation of '{m.group('who').lower()}', not a person"
    if _WOULD_BE.search(s):
        return None, False, "stated as a wish, no owner named"
    m = _NAME_WILL.search(s)
    if m and m.group("name").lower() not in NOT_NAMES:
        action = m.group("action").lower()
        if not action.startswith(("be ", "be.", "been ", "like ", "love ")):
            return m.group("name"), False, None
    return "__none__"


def extract_rules(utterances, silence_after) -> list:
    attendees = build_attendees(utterances)
    out = []
    for u in utterances:
        for sent in split_sentences(u.text):
            res = classify_sentence(sent, u.speaker, u.side, attendees)
            if res == "__none__":
                continue
            owner_raw, inferred, note = res
            _, due_raw = find_due_phrase(sent)
            cand = Candidate(u.line_no, u.speaker, sent, owner_raw, due_raw,
                             bool(HEDGE_RE.search(sent)), inferred)
            if note:
                cand.notes.append(note)
            if u.line_no in silence_after and owner_raw in (None, "we", "someone", "somebody", "anyone"):
                cand.notes.append("no one responded in the transcript")
            out.append(cand)
    return out


# --------------------------------------------------------------------------
# Optional LLM extraction (the LLM only EXTRACTS; validation stays deterministic)
# --------------------------------------------------------------------------
LLM_SYSTEM = (
    "You extract commitments from meeting transcripts. A commitment is a sentence where someone "
    "promises, is asked, or is said to need to do something. Do NOT include status updates or "
    "opinions. Return ONLY a JSON array, no prose, no markdown. Each item: "
    '{"line": <int line number>, "speaker": <string or null>, "sentence": <EXACT text copied from '
    'the transcript>, "owner": <name of the single person responsible, or null if none is named; '
    'use null for "we"/"someone">, "due": <the time expression exactly as written, or null>, '
    '"hedged": <true if the wording is tentative, e.g. maybe/try/look into>}. '
    "Never invent a sentence, owner or date that is not in the transcript."
)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def parse_llm_items(raw_text: str, transcript_text: str):
    """Turn the model's reply into Candidates. Returns (candidates, dropped_count).
    Items whose sentence is not found verbatim in the transcript are dropped."""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_text.strip(), flags=re.I)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise InputError(f"the LLM reply was not valid JSON ({e.msg}); falling back is recommended.")
    if not isinstance(data, list):
        raise InputError("the LLM reply was not a JSON array.")
    hay = _norm(transcript_text)
    cands, dropped = [], 0
    for it in data:
        if not isinstance(it, dict) or not isinstance(it.get("sentence"), str):
            dropped += 1
            continue
        if _norm(it["sentence"]) not in hay:
            dropped += 1  # hallucinated or paraphrased: not verifiable
            continue
        owner = it.get("owner") if isinstance(it.get("owner"), str) and it["owner"].strip() else None
        due = it.get("due") if isinstance(it.get("due"), str) and it["due"].strip() else None
        line = it.get("line") if isinstance(it.get("line"), int) else 0
        spk = it.get("speaker") if isinstance(it.get("speaker"), str) else None
        cands.append(Candidate(line, spk, it["sentence"].strip(), owner, due,
                               bool(it.get("hedged")) or bool(HEDGE_RE.search(it["sentence"]))))
    return cands, dropped


def extract_llm(utterances, transcript_text: str, api_key: str, model: str):
    numbered = "\n".join(f"{u.line_no}: {(u.speaker + ': ') if u.speaker else ''}{u.text}" for u in utterances)
    body = json.dumps({"model": model, "max_tokens": 4000, "system": LLM_SYSTEM,
                       "messages": [{"role": "user", "content": numbered}]}).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body, method="POST",
        headers={"content-type": "application/json", "x-api-key": api_key,
                 "anthropic-version": "2023-06-01"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.load(resp)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        raise InputError(f"LLM request failed: {e}")
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    return parse_llm_items(text, transcript_text)


# --------------------------------------------------------------------------
# Validation (deterministic)
# --------------------------------------------------------------------------
def resolve_owner(owner_raw: Optional[str], attendees: dict):
    """Return (display name or None, side or None, note or None)."""
    if not owner_raw:
        return None, None, None
    key = owner_raw.strip().lower()
    if key in GENERIC_OWNERS:
        return None, None, None
    if key in attendees:
        return attendees[key][0], attendees[key][1], None
    first = key.split()[0]
    if first in attendees:
        return attendees[first][0], attendees[first][1], None
    return owner_raw.strip(), "unknown", None


def validate(cands, meeting: date, attendees: dict) -> list:
    results = []
    for n, c in enumerate(cands, 1):
        issues, notes = [], list(c.notes)
        owner, owner_side, _ = resolve_owner(c.owner_raw, attendees)
        if owner is None:
            issues.append("MISSING_OWNER")
        due_text = due_date = None
        if c.due_raw:
            due = resolve_due(c.due_raw, meeting)
            due_text = due.raw
            if due.problem == "imprecise":
                issues.append("MISSING_DATE")
                notes.append(due.note)
            elif due.problem in ("invalid", "before_meeting"):
                issues.append("BAD_DATE")
                notes.append(due.note)
            else:
                due_date = due.resolved.isoformat()
                notes.append(f"due {due.note}")
        else:
            issues.append("MISSING_DATE")
            notes.append("no time expression found")
        if c.hedged:
            issues.append("VAGUE_ACTION")
        if "MISSING_OWNER" in issues and HARD_RE.search(c.sentence):
            issues.append("UNCLAIMED_HARD")
        status = "complete"
        for key in STATUS_PRIORITY:
            if key in issues:
                status = STATUS_NAME[key]
                break
        results.append(Commitment(
            id=n, line=c.line_no, speaker=c.speaker, sentence=c.sentence, owner=owner,
            owner_side=owner_side, due_text=due_text, due_date=due_date, status=status,
            issues=issues, notes=notes, fixes=[FIX_TEXT[i] for i in STATUS_PRIORITY if i in issues]))
    return results


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------
def summarize(items) -> dict:
    total = len(items)
    complete = sum(1 for i in items if i.status == "complete")
    counts = {k: sum(1 for i in items if k in i.issues) for k in ISSUE_LABEL}
    return {"total": total, "complete": complete, "needs_attention": total - complete,
            "percent_complete": round(100 * complete / total) if total else None,
            "issue_counts": counts}


def followup_draft(title: Optional[str], meeting: date, items) -> str:
    ok = [i for i in items if i.status == "complete"]
    todo = [i for i in items if i.status != "complete"]
    lines = [f"Subject: Follow-up: {title or 'meeting'} - next steps ({meeting.isoformat()})", "",
             "Hi all,", "", "Thanks for the time today. Here is what we captured.", ""]
    if ok:
        lines.append("Confirmed (owner and date agreed):")
        for i in ok:
            lines.append(f'- {i.owner}, due {i.due_date}: "{i.sentence}"')
        lines.append("")
    if todo:
        lines.append("Still open - please reply with what is missing:")
        for i in todo:
            need = ", ".join({"MISSING_OWNER": "an owner", "MISSING_DATE": "a due date",
                              "BAD_DATE": "a corrected date", "VAGUE_ACTION": "a concrete deliverable",
                              "UNCLAIMED_HARD": "a lead or a smaller scope"}[k]
                             for k in STATUS_PRIORITY if k in i.issues)
            lines.append(f'- "{i.sentence}" - needs {need}')
        lines.append("")
    if not items:
        lines.append("We did not record any specific commitments.")
        lines.append("")
    lines += ["Best,", "[your name]"]
    return "\n".join(lines)


def render_text(title, meeting, extractor, items, summ, draft, extra_notes) -> str:
    w = 78
    out = ["=" * w, f"COMMITMENT GAP CHECK  -  {title or 'untitled meeting'}",
           f"Meeting date: {meeting.isoformat()} ({meeting.strftime('%A')})   Extractor: {extractor}",
           "=" * w]
    for n in extra_notes:
        out.append(f"NOTE: {n}")
    if not items:
        out += ["", "No commitments detected.",
                "If you expected some, check that lines look like 'Name (Side): text' and are in English.", ""]
    else:
        c = summ["issue_counts"]
        out += ["", f"{summ['total']} commitments found: {summ['complete']} complete "
                    f"({summ['percent_complete']}%), {summ['needs_attention']} need attention.",
                f"  missing owner: {c['MISSING_OWNER']}   missing/imprecise date: {c['MISSING_DATE']}   "
                f"vague wording: {c['VAGUE_ACTION']}   date problem: {c['BAD_DATE']}   "
                f"unclaimed large items: {c['UNCLAIMED_HARD']}", ""]
        hard = [i for i in items if "UNCLAIMED_HARD" in i.issues]
        noowner = [i for i in items if "MISSING_OWNER" in i.issues and "UNCLAIMED_HARD" not in i.issues]
        if hard or noowner:
            out += ["REVIEW BEFORE THE MEETING ENDS (nobody owns these):"]
            for i in hard + noowner:
                tag = "LARGE" if "UNCLAIMED_HARD" in i.issues else "     "
                out += textwrap.wrap(f'[{i.id}] {tag} "{i.sentence}"', w,
                                     initial_indent="  ", subsequent_indent="        ")
            out.append("")
        out += ["-" * w, "DETAIL", "-" * w]
        for i in items:
            label = "COMPLETE" if i.status == "complete" else "NEEDS ATTENTION: " + ", ".join(
                ISSUE_LABEL[k] for k in STATUS_PRIORITY if k in i.issues)
            out.append(f"[{i.id}] line {i.line}  {label}")
            out += textwrap.wrap(f'"{i.sentence}"', w, initial_indent="    ", subsequent_indent="     ")
            owner = f"{i.owner} ({i.owner_side})" if i.owner else "-"
            out.append(f"    owner: {owner}   due: {i.due_date or '-'}")
            for n in i.notes:
                out += textwrap.wrap(f"note: {n}", w, initial_indent="    ", subsequent_indent="          ")
            for f_ in i.fixes:
                out += textwrap.wrap(f"fix: {f_}", w, initial_indent="    ", subsequent_indent="         ")
            out.append("")
    if draft:
        out += ["-" * w, "DRAFT FOLLOW-UP MESSAGE (review before sending)", "-" * w, draft, ""]
    return "\n".join(out)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def analyse(text: str, date_arg: Optional[str] = None, use_llm: bool = False,
            api_key: Optional[str] = None, model: str = "claude-sonnet-5-5") -> dict:
    validate_text(text)
    title, header_date = parse_header(text)
    date_str = date_arg or header_date
    if not date_str:
        raise InputError("no meeting date. Add a line 'Meeting date: YYYY-MM-DD' to the file or pass --date.")
    meeting = parse_date_arg(date_str)
    utterances, silence_after = parse_transcript(text)
    attendees = build_attendees(utterances)
    notes, extractor = [], "rules (offline)"
    if use_llm:
        if not api_key:
            notes.append("--llm requested but ANTHROPIC_API_KEY is not set; used the offline rule-based extractor.")
            cands = extract_rules(utterances, silence_after)
        else:
            cands, dropped = extract_llm(utterances, text, api_key, model)
            extractor = f"LLM ({model}) + deterministic validation"
            if dropped:
                notes.append(f"{dropped} LLM item(s) were dropped because their text was not found verbatim in the transcript.")
    else:
        cands = extract_rules(utterances, silence_after)
    items = validate(cands, meeting, attendees)
    return {"title": title, "meeting": meeting, "extractor": extractor, "items": items,
            "summary": summarize(items), "notes": notes}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Find commitments in a meeting transcript that lack an owner, a real date, or a clear action.")
    ap.add_argument("file", help="path to transcript text file, or '-' for stdin")
    ap.add_argument("--date", help="meeting date YYYY-MM-DD (overrides 'Meeting date:' header)")
    ap.add_argument("--format", choices=["text", "json"], default="text")
    ap.add_argument("--llm", action="store_true", help="use an LLM for extraction (needs ANTHROPIC_API_KEY)")
    ap.add_argument("--model", default=os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5"))
    ap.add_argument("--no-followup", action="store_true", help="omit the draft follow-up message")
    ap.add_argument("--fail-on-gaps", action="store_true", help="exit with code 1 if any commitment needs attention")
    ap.add_argument("--version", action="version", version=__version__)
    args = ap.parse_args(argv)
    try:
        if args.file == "-":
            text = sys.stdin.read()
        else:
            try:
                with open(args.file, "rb") as fh:
                    text = fh.read().decode("utf-8")
            except FileNotFoundError:
                raise InputError(f"file not found: {args.file}")
            except UnicodeDecodeError:
                raise InputError("the file is not valid UTF-8 text (it may be binary or corrupted).")
        r = analyse(text, args.date, args.llm, os.environ.get("ANTHROPIC_API_KEY"), args.model)
    except InputError as e:
        print(f"Input error: {e}", file=sys.stderr)
        return 2
    draft = None if args.no_followup else followup_draft(r["title"], r["meeting"], r["items"])
    if args.format == "json":
        print(json.dumps({"meeting": r["title"], "meeting_date": r["meeting"].isoformat(),
                          "extractor": r["extractor"], "notes": r["notes"], "summary": r["summary"],
                          "commitments": [asdict(i) for i in r["items"]], "followup_draft": draft},
                         indent=2, ensure_ascii=False))
    else:
        print(render_text(r["title"], r["meeting"], r["extractor"], r["items"], r["summary"], draft, r["notes"]))
    if args.fail_on_gaps and r["summary"]["needs_attention"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
