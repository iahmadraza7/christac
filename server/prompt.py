"""
System prompt assembly, stage resolution and verdict detection.

The assembly rule, in order:

    1. courtship-decoder-instructions.md        always
    2. the ONE decode file for the stage        always
    3. the matching L1-L5 lesson file           ONLY after a verdict

The order matters for cost. Parts 1 and 2 are the same on every turn of a
conversation, so they form a stable prefix the provider can cache. The lesson
file is appended at the end, after a verdict, so switching it on does not
invalidate the cached prefix — only the new tail is billed at full rate.

Lesson files are 90KB-137KB. Loading one before a verdict would triple the cost
of every turn for no benefit: the lessons are unreachable before a verdict by
design.
"""
from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

INSTRUCTIONS_FILE = "courtship-decoder-instructions.md"
# Deduplicated lesson source used by the service. Each lesson is stored
# once here; the L1-L5 files remain untouched for her GPT.
LESSON_DIR = "server/lessons"
NEWLINE = chr(10)

# Stage keys are internal. Stage 2 carries a phase because the decode files
# split it and the instructions forbid judging a Phase 1 woman by Phase 2.
STAGE_1, STAGE_2_P1, STAGE_2_P2, STAGE_3, STAGE_4 = "1", "2p1", "2p2", "3", "4"
STAGES = [STAGE_1, STAGE_2_P1, STAGE_2_P2, STAGE_3, STAGE_4]

DECODE_FILE = {
    STAGE_1:    "01-stage-1-before-first-date.md",
    STAGE_2_P1: "02-stage-2-phase-1.md",
    STAGE_2_P2: "03-stage-2-phase-2.md",
    STAGE_3:    "04-stage-3-standard-to-proposal.md",
    STAGE_4:    "05-stage-4-engagement-to-altar.md",
}

LESSON_FILE = {
    STAGE_1:    "L1-stage-1-lessons.md",
    STAGE_2_P1: "L2-stage-2-phase-1-lessons.md",
    STAGE_2_P2: "L3-stage-2-phase-2-lessons.md",
    STAGE_3:    "L4-stage-3-lessons.md",
    STAGE_4:    "L5-stage-4-lessons.md",
}

# Same delimiter the offline harness uses, so what runs here matches what was
# tested. Plain on purpose: it names the file and says nothing else.
DELIMITER = "\n\n" + "=" * 70 + "\nKNOWLEDGE FILE: {name}\n" + "=" * 70 + "\n\n"


# ---------------------------------------------------------------------------
# File loading, cached on mtime so edits are picked up without a restart
# ---------------------------------------------------------------------------
_cache: dict[str, tuple[float, str]] = {}
_lock = threading.Lock()


def read_file(name: str) -> str:
    path = ROOT / name
    mtime = path.stat().st_mtime
    with _lock:
        hit = _cache.get(name)
        if hit and hit[0] == mtime:
            return hit[1]
    text = path.read_text(encoding="utf-8")
    with _lock:
        _cache[name] = (mtime, text)
    return text


def preflight() -> list[str]:
    """Every file the service can ever need. Called once at startup."""
    missing = []
    parts = [f"{LESSON_DIR}/manifest.json"]
    for name in [INSTRUCTIONS_FILE, *DECODE_FILE.values(), *LESSON_FILE.values(),
                 *parts]:
        if not (ROOT / name).is_file():
            missing.append(name)
    return missing


# ---------------------------------------------------------------------------
# Stage resolution
# ---------------------------------------------------------------------------
_STAGE_WORD = re.compile(r"\bstage\s*(?:#\s*)?([1-4])\b", re.I)
_PHASE_WORD = re.compile(r"\bphase\s*([12])\b", re.I)
_WORD_NUMBER = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "a couple of": 2, "a couple": 2, "a few": 3, "several": 4,
}
_DATE_COUNT = re.compile(
    r"\b(\d{1,2}|" + "|".join(_WORD_NUMBER) + r")\s+dates?\b", re.I)


def _date_count(text: str) -> int | None:
    best = None
    for m in _DATE_COUNT.finditer(text):
        raw = m.group(1).lower()
        n = int(raw) if raw.isdigit() else _WORD_NUMBER.get(raw)
        if n is not None:
            best = n if best is None else max(best, n)
    return best


def resolve_stage(user_text: str, current: str | None) -> str:
    """
    Work out which single decode file is in play.

    `user_text` is everything she has said so far in this conversation, oldest
    first. The stage is sticky: it only changes when she names a different one,
    or when a date count moves Stage 2 from Phase 1 to Phase 2.

    Before she says anything that names a stage, this returns Stage 1. The
    assistant's own first move is to ask her the stage, so the file is swapped
    on the next turn once she answers.
    """
    stage_hits = _STAGE_WORD.findall(user_text)
    named = stage_hits[-1] if stage_hits else None

    if named == "2" or (named is None and current in (STAGE_2_P1, STAGE_2_P2)):
        # Phase by explicit words first, then by date count.
        phases = _PHASE_WORD.findall(user_text)
        if phases:
            return STAGE_2_P1 if phases[-1] == "1" else STAGE_2_P2
        n = _date_count(user_text)
        if n is not None:
            return STAGE_2_P1 if n <= 2 else STAGE_2_P2
        # "If it is unclear, ask how many times they have been out." Phase 1 is
        # the safe default: it is where a Stage 2 conversation begins.
        return current if current in (STAGE_2_P1, STAGE_2_P2) else STAGE_2_P1

    if named == "1":
        return STAGE_1
    if named == "3":
        return STAGE_3
    if named == "4":
        return STAGE_4

    return current or STAGE_1


# ---------------------------------------------------------------------------
# Verdict detection
#
# The instructions forbid the assistant from labelling its output, so a reply
# never contains the literal string "DELIVER AS WRITTEN". What it contains,
# when a verdict lands, is the *body* of one of those blocks with her man's
# details swapped in. So detection compares the reply against the block bodies
# of the decode file that was actually in play.
#
# Measured against 26 real turns from results/: delivered blocks score 0.90 to
# 1.00, everything else scores 0.06 or below. The threshold sits in that gap.
# ---------------------------------------------------------------------------
_BLOCK_HEAD = re.compile(r"^### DELIVER AS WRITTEN\b.*$", re.M)
_ANY_HEAD = re.compile(r"^#{2,3} ", re.M)
_SHINGLE_N = 5


def verdict_block_items(stage: str) -> list[tuple[str, str]]:
    """
    Every DELIVER AS WRITTEN block in one decode file, as (name, body).

    The name is the heading with its marker stripped, which is what identifies
    a block once it has been delivered - "Fail response - Stage 1, Marriage
    Readiness Filter: Dating Profile" and so on.
    """
    text = read_file(DECODE_FILE[stage])
    heads = list(_BLOCK_HEAD.finditer(text))
    out = []
    for i, m in enumerate(heads):
        end = len(text)
        nxt = _ANY_HEAD.search(text, m.end())
        if nxt:
            end = nxt.start()
        if i + 1 < len(heads):
            end = min(end, heads[i + 1].start())
        body = text[m.end():end].strip().strip("-").strip()
        if body:
            name = m.group(0).replace("### DELIVER AS WRITTEN", "").strip()
            name = name.lstrip("-").lstrip().lstrip("\u2014").strip()
            out.append((name, body))
    return out


def verdict_blocks(stage: str) -> list[str]:
    """Body text of every DELIVER AS WRITTEN block in one decode file."""
    return [body for _name, body in verdict_block_items(stage)]


def _tokens(s: str) -> list[str]:
    s = s.lower().replace("’", "'").replace("…", " ")
    return re.sub(r"[^a-z0-9' ]+", " ", s).split()


def _shingles(tokens: list[str]) -> set[tuple]:
    n = _SHINGLE_N
    return {tuple(tokens[i:i + n]) for i in range(len(tokens) - n + 1)}


def match_verdict(reply: str, stage: str) -> tuple[str | None, float]:
    """
    Which written block this reply delivered, and how much of it, 0.0 to 1.0.

    Containment, not similarity: the reply may add her man's details and still
    count, but it has to carry the block's own wording to score. Knowing WHICH
    block landed is what lets the service stop it being delivered twice for the
    same man - the model is never asked to remember what it already said.
    """
    reply_sh = _shingles(_tokens(reply))
    if not reply_sh:
        return None, 0.0
    best_name, best = None, 0.0
    for name, block in verdict_block_items(stage):
        block_sh = _shingles(_tokens(block))
        if not block_sh:
            continue
        score = len(block_sh & reply_sh) / len(block_sh)
        if score > best:
            best_name, best = name, score
    return best_name, best


def verdict_score(reply: str, stage: str) -> float:
    """How much of the closest written block this reply reproduces."""
    return match_verdict(reply, stage)[1]


def already_said_section(names: list[str]) -> str:
    """
    The blocks already given for the man in play, listed for the model.

    Kept last in the prompt and small, so the cached prefix and the lesson
    block above it are untouched and this costs a few tokens a turn.
    """
    if not names:
        return ""
    listed = NEWLINE.join(f"- {n}" for n in names)
    return (
        NEWLINE * 2 + "=" * 70 + NEWLINE        + "ALREADY DELIVERED FOR THIS MAN" + NEWLINE        + "=" * 70 + NEWLINE * 2
        + "You have already given this man these written responses, so they have "
          "been said and cannot be said again:" + NEWLINE * 2 + listed + NEWLINE * 2
        + "Follow the rule in the instructions: do not repeat any of them. A "
          "written response not on this list is still delivered exactly as "
          "written."
    )


# ---------------------------------------------------------------------------
# Repeat decode detection
#
# Her method ends after the verdict and the next step. If she comes back and
# describes the same man again, re-decoding costs money and cannot reach a
# different answer: the verdict follows his behaviour, and she has not given
# any new behaviour. So the service answers without calling the model at all.
#
# This must NOT fire on two things that look superficially similar:
#   - pushback ("but he's been busy"), which the instructions handle explicitly
#     and which needs the model
#   - a new man, which is a fresh decode and must run normally
# Both are caught by the same test: pushback and a new man both carry wording
# she has not used before, so their overlap with what she already said is low.
# ---------------------------------------------------------------------------
_NEW_MAN = re.compile(
    r"\b(another (man|guy|one)|different (man|guy|one)|someone else|new (man|guy)|"
    r"next (man|guy|one)|a different situation|other man)\b", re.I)


def mentions_a_new_man(text: str) -> bool:
    return bool(_NEW_MAN.search(text))


def repeats_earlier_message(message: str, earlier: list[str],
                            threshold: float) -> float:
    """
    How much of this message she has already told us, 0.0 to 1.0.

    Containment of the new message's wording inside what she said before, so a
    restatement scores high even when she words it more briefly the second time.
    """
    new = _shingles(_tokens(message))
    if not new:
        return 0.0
    seen = set()
    for m in earlier:
        seen |= _shingles(_tokens(m))
    if not seen:
        return 0.0
    return len(new & seen) / len(new)


# ---------------------------------------------------------------------------
# Lesson composition
#
# The L1-L5 files stay exactly as they are, because her GPT can only load one
# file and needs every lesson for a stage in one place. The service has no such
# limit, so it stores each lesson once and composes the stage's set on demand —
# in her original order, so what the model sees is unchanged.
# ---------------------------------------------------------------------------
_manifest_cache: dict | None = None


def manifest() -> dict:
    global _manifest_cache
    if _manifest_cache is None:
        _manifest_cache = json.loads(read_file(f"{LESSON_DIR}/manifest.json"))
    return _manifest_cache


def compose_lessons(stage: str) -> str:
    entry = manifest()[stage]
    label = entry["label"]
    blocks = []
    for item in entry["lessons"]:
        body = read_file(f"{LESSON_DIR}/{item['file']}").strip()
        blocks.append(
            f"## FOLLOW UP — {label} — {item['title']}" + NEWLINE * 2
            + "USE: after a verdict only. Never to reach one." + NEWLINE * 2
            + body)
    return (NEWLINE * 2).join(blocks)


# ---------------------------------------------------------------------------
# Her positioning names
#
# The instructions ask the assistant, after a verdict, to name the High League
# Positioning her own behaviour broke - in her wording, taken from the lessons,
# never from memory. But the lessons only load after a verdict, so on the turn
# the verdict lands the names are not in front of it and it correctly says
# nothing. Measured: of two live test cases, one named the positioning and the
# other gave the right coaching without ever naming it.
#
# So the names alone - five short headings, no teaching body - are lifted out
# of her lesson file and put in the always-loaded part of the prompt. Each one
# describes what SHE does, never what he does, so they cannot tilt a verdict
# about his behaviour. That is why this does not breach the rule that lesson
# material stays out until a verdict.
#
# Web app only. Her ChatGPT cannot inject anything per request and its
# instructions field is near the 8,000 character limit, so that file is left
# exactly as it is.
# ---------------------------------------------------------------------------
_POSITIONING = re.compile(r"POSITIONING\s*([1-9])\s*[:\-–—]?\s*(.+)", re.I)

# Above this length the heading has her teaching running on after the name.
_NAME_MAX = 60


def _positioning_name(raw: str) -> str:
    """
    Her name for one positioning, without the teaching that follows it.

    At Stages 3 and 4 the heading sits on its own line and is taken whole. At
    Stages 1 and 2 her source runs the body straight on after the name, so the
    leading run of shouted words is the name and the first ordinary word ends
    it.
    """
    raw = raw.strip()
    if len(raw) <= _NAME_MAX:
        return raw.strip(" .-–—")
    kept = []
    for word in raw.split():
        if any(c.islower() for c in word):
            break
        kept.append(word)
    name = " ".join(kept).strip(" .-–—")
    return name or raw[:_NAME_MAX].strip()


def positioning_names(stage: str) -> list[str]:
    """The positioning names for this stage, in her wording, in her order."""
    text = compose_lessons(stage)
    found: dict[str, str] = {}
    for line in text.split(NEWLINE):
        for m in _POSITIONING.finditer(line):
            n = m.group(1)
            if n not in found:
                name = _positioning_name(m.group(2))
                if name:
                    found[n] = name
    return [found[k] for k in sorted(found)]


def positioning_block(stage: str) -> str:
    """The names, framed so it knows they are hers and what they are for."""
    names = positioning_names(stage)
    if not names:
        return ""
    listed = NEWLINE.join(f"{i}. {n}" for i, n in enumerate(names, start=1))
    intro = (
        "These are the High League Positioning names from her lessons for this "
        "stage, in her wording. They are in front of you now." + NEWLINE * 2
        + "Use them only for the positioning rule in the instructions, after a "
        "verdict. Each one describes what SHE does, never what he does, so they "
        "play no part in reaching a verdict about him." + NEWLINE * 2
    )
    return (DELIMITER.format(name="HIGH LEAGUE POSITIONINGS FOR THIS STAGE")
            + intro + listed)


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------
@dataclass
class SystemPrompt:
    """
    The prompt in parts, so each can carry its own cache decision.

    prefix   instructions + the one decode file. Stable for the whole
             conversation, so always worth caching.
    lesson   the stage's lessons, composed from the deduplicated source in her
             original order. None until a verdict lands, and always appended
             after the prefix so switching it on leaves the cached prefix alone.
    """
    prefix: str
    lesson: str | None
    stage: str
    already_said: str | None = None

    @property
    def parts(self) -> list[str]:
        return [x for x in (self.prefix, self.lesson, self.already_said) if x]

    @property
    def text(self) -> str:
        return "".join(self.parts)

    @property
    def chars(self) -> int:
        return len(self.text)


def build(stage: str, verdict_delivered: bool,
          blocks_delivered: list[str] | None = None) -> SystemPrompt:
    instructions = read_file(INSTRUCTIONS_FILE).rstrip()
    decode_name = DECODE_FILE[stage]
    decode = read_file(decode_name).strip()
    prefix = (instructions + DELIMITER.format(name=decode_name) + decode
              + positioning_block(stage))

    lesson = None
    if verdict_delivered:
        lesson = DELIMITER.format(name=LESSON_FILE[stage]) + compose_lessons(stage)

    return SystemPrompt(prefix=prefix, lesson=lesson, stage=stage,
                        already_said=already_said_section(blocks_delivered or [])
                        or None)
