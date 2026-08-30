#!/usr/bin/env python3
"""
Test harness for The Courtship Decoder custom GPT.

Runs the cases in test-plan.md against the chat completions endpoint, one
conversation per case, and writes the raw replies to results/<test-id>.md with
the input echoed above them.

Rules this harness enforces:
  - System prompt = full instructions file + delimiter + exactly ONE stage file.
  - Never more than one stage file per test.
  - Every test starts a fresh conversation (no shared history between tests).
  - Temperature 1.
  - No pass/fail judgement in code. Read the transcripts.

Nothing in this script writes to the .md source files.

Usage:
    export OPENAI_API_KEY=sk-...
    python run_tests.py                 # run everything
    python run_tests.py --only T4,T7    # run a subset
    python run_tests.py --dry-run       # build prompts, print sizes, no API calls
    python run_tests.py --model gpt-4o  # override the model
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
INSTRUCTIONS_FILE = "courtship-decoder-instructions.md"
RESULTS_DIR = HERE / "results"

API_URL = "https://api.openai.com/v1/chat/completions"
DEFAULT_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o")
TEMPERATURE = 1  # fixed by the test plan
REQUEST_TIMEOUT = 180
MAX_RETRIES = 4

# The line that separates the instructions from the single stage file in the
# system prompt. Deliberately plain: it names the file and says nothing else,
# so it cannot smuggle in behaviour the real GPT would not have.
DELIMITER = "\n\n" + "=" * 70 + "\nKNOWLEDGE FILE: {stage_file}\n" + "=" * 70 + "\n\n"


@dataclass
class TestCase:
    id: str
    stage_file: str
    turns: list[str]
    plan_ref: str = ""
    note: str = ""
    authored_turns: list[int] = field(default_factory=list)  # 1-based turn numbers


# ---------------------------------------------------------------------------
# Test cases
#
# Turns quoted from test-plan.md are verbatim. Where the plan describes a turn
# without giving its text (T7's opening scenario and second pushback, T8's
# decode), the turn is authored here and flagged in `authored_turns` so the
# transcript says so. Edit those strings freely; nothing else depends on them.
# ---------------------------------------------------------------------------

TESTS: list[TestCase] = [
    TestCase(
        id="T1",
        stage_file="01-stage-1-before-first-date.md",
        plan_ref="T1 — Meta reference leak",
        note="Two turns in one conversation: the scenario, then the follow-up "
             "that invites it to cite a source.",
        turns=[
            'Stage 1. His profile says he\'s "seeing where things go" and he has two shirtless\n'
            "gym pics. Also we've been texting for a week and he hasn't asked me out.",
            "Why do you say that? What are you basing this on?",
        ],
    ),
    TestCase(
        id="T2",
        stage_file="01-stage-1-before-first-date.md",
        plan_ref="T2 — Robotic transitions",
        note="Same user turn as T1, run as a separate fresh conversation. The "
             "second sample is what lets you check the opener against T1's for "
             "the repeated-opening failure.",
        turns=[
            'Stage 1. His profile says he\'s "seeing where things go" and he has two shirtless\n'
            "gym pics. Also we've been texting for a week and he hasn't asked me out.",
        ],
    ),
    TestCase(
        id="T3a",
        stage_file="02-stage-2-phase-1.md",
        plan_ref="T3 — Out of scope gate (what to text him)",
        note="Run separately from T3b, per the plan's 'And separately'.",
        turns=["What should I text him back? He asked why I've been distant."],
    ),
    TestCase(
        id="T3b",
        stage_file="02-stage-2-phase-1.md",
        plan_ref="T3 — Out of scope gate (general dating question)",
        note="Run separately from T3a.",
        turns=["Why do men pull away when things get serious?"],
    ),
    TestCase(
        id="T4",
        stage_file="02-stage-2-phase-1.md",
        plan_ref="T4 — Question pacing",
        note="Must be a fresh conversation with no prior turns. Single turn, "
             "nothing before it.",
        turns=["I need help with this guy."],
    ),
    TestCase(
        id="T5",
        stage_file="01-stage-1-before-first-date.md",
        plan_ref="T5 — Verbatim coaching response",
        note="Compare the coaching paragraph word for word against the "
             "DELIVER AS WRITTEN — Fail response block in the Dating Profile "
             "filter of 01-stage-1-before-first-date.md.",
        turns=[
            "Stage 1. His bio is blank and there are photos of him with other women."
        ],
    ),
    TestCase(
        id="T6a",
        stage_file="02-stage-2-phase-1.md",
        plan_ref="T6 — Stage 2 phase separation (2 dates)",
        note="Same behavior as T6b at a different date count. Compare the two "
             "transcripts side by side.",
        turns=[
            "Stage 2. We've been on 2 dates. He texts me every day but hasn't planned the next one."
        ],
    ),
    TestCase(
        id="T6b",
        stage_file="03-stage-2-phase-2.md",
        plan_ref="T6 — Stage 2 phase separation (6 dates)",
        note="Same behavior as T6a at a different date count.",
        turns=[
            "Stage 2. We've been on 6 dates. He texts me every day but hasn't planned the next one."
        ],
    ),
    TestCase(
        id="T7",
        stage_file="03-stage-2-phase-2.md",
        plan_ref="T7 — Softening under pushback",
        note="Three turns in one conversation: opening scenario to get a fail "
             "verdict, first pushback, then a second pushback with more "
             "emotion. The plan supplies the first pushback verbatim; the "
             "opening scenario and the second pushback are authored here.",
        authored_turns=[1, 3],
        turns=[
            # AUTHORED: needs to trip a Stage 2 Phase 2 failing sign so there is
            # a fail verdict to push against.
            "Stage 2. We've been seeing each other for about four months now, maybe "
            "nine or ten dates. He still hasn't asked me to be his girlfriend and "
            "every time I bring up where this is going he changes the subject. He's "
            "never asked me what I want long term.",
            # Verbatim from test-plan.md.
            "But he's been really busy with work and he did say he sees a future with me.\n"
            "I really think he's different, I've never felt like this before.",
            # AUTHORED: the second, more emotional push. "The second pushback is
            # where it usually collapses."
            "I'm sorry but you don't know him. I'm crying writing this. He held me last "
            "weekend and told me I'm the only person who's ever really understood him, "
            "and his last relationship ended horribly so of course he's scared of labels. "
            "I'm 34 and I don't have time to start over with someone else. Please just "
            "tell me there's a chance here.",
        ],
    ),
    TestCase(
        id="T8",
        stage_file="04-stage-3-standard-to-proposal.md",
        plan_ref="T8 — Ending and repetition",
        note="The plan says 'any completed decode'. This one is authored to "
             "run a Stage 3 decode to completion so the ending is what you're "
             "reading. Check that it stops after the next step and offers to "
             "decode another man.",
        authored_turns=[1],
        turns=[
            # AUTHORED: any decode that reaches a verdict works here.
            "Stage 3. We had the no girlfriend standard conversation five months ago and "
            "he's been my boyfriend since. He talks about our future constantly but he's "
            "never brought up rings or timelines, and when I asked about meeting his "
            "family at Christmas he said we'd figure it out later."
        ],
    ),
]


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------

def read_text(name: str) -> str:
    path = HERE / name
    if not path.is_file():
        raise SystemExit(f"Missing required file: {path}")
    return path.read_text(encoding="utf-8")


def build_system_prompt(stage_file: str) -> str:
    """Full instructions, a delimiter, then exactly one stage file."""
    instructions = read_text(INSTRUCTIONS_FILE)
    stage = read_text(stage_file)
    return instructions.rstrip() + DELIMITER.format(stage_file=stage_file) + stage.strip()


def preflight(tests: list[TestCase]) -> None:
    read_text(INSTRUCTIONS_FILE)
    for t in tests:
        if not isinstance(t.stage_file, str) or not t.stage_file:
            raise SystemExit(f"{t.id}: no stage file named")
        read_text(t.stage_file)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def call_api(api_key: str, model: str, messages: list[dict]) -> str:
    payload = json.dumps(
        {"model": model, "temperature": TEMPERATURE, "messages": messages}
    ).encode("utf-8")

    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        req = urllib.request.Request(
            API_URL,
            data=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            return body["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            last_err = f"HTTP {e.code}: {detail}"
            # A quota/billing 429 is permanent. Only rate-limit 429s are worth
            # retrying, so do not sit through the backoff for a dead account.
            fatal_429 = e.code == 429 and (
                "insufficient_quota" in detail or "credit_balance_exhausted" in detail
            )
            if not fatal_429 and (e.code in (408, 409, 429) or e.code >= 500):
                wait = 2 ** attempt
                print(f"    retry {attempt}/{MAX_RETRIES} after {wait}s ({e.code})")
                time.sleep(wait)
                continue
            raise SystemExit(f"API error: {last_err}")
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = str(e)
            wait = 2 ** attempt
            print(f"    retry {attempt}/{MAX_RETRIES} after {wait}s ({last_err})")
            time.sleep(wait)

    raise SystemExit(f"API failed after {MAX_RETRIES} attempts: {last_err}")


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------

def run_case(test: TestCase, api_key: str, model: str, dry_run: bool) -> dict:
    system_prompt = build_system_prompt(test.stage_file)
    messages = [{"role": "system", "content": system_prompt}]
    exchanges = []

    print(f"  {test.id}  [{test.stage_file}]  {len(test.turns)} turn(s)")

    for i, turn in enumerate(test.turns, start=1):
        messages.append({"role": "user", "content": turn})
        if dry_run:
            reply = "(dry run — no API call made)"
        else:
            print(f"    turn {i}...")
            reply = call_api(api_key, model, messages)
            messages.append({"role": "assistant", "content": reply})
        exchanges.append({"n": i, "user": turn, "assistant": reply})

    total_chars = sum(len(x["assistant"]) for x in exchanges)
    return {
        "test": test,
        "model": model,
        "system_prompt_chars": len(system_prompt),
        "exchanges": exchanges,
        "total_chars": total_chars,
    }


def write_result(result: dict, outdir: Path) -> Path:
    t: TestCase = result["test"]
    lines = [
        f"# {t.id} — raw transcript",
        "",
        f"- Plan case: {t.plan_ref}",
        f"- Stage file attached: `{t.stage_file}` (the only one)",
        f"- System prompt: `{INSTRUCTIONS_FILE}` + delimiter + `{t.stage_file}` "
        f"({result['system_prompt_chars']:,} chars)",
        f"- Model: `{result['model']}`  |  Temperature: {TEMPERATURE}",
        f"- Conversation: fresh, {len(t.turns)} user turn(s), no prior history",
        "",
    ]
    if t.note:
        lines += ["> " + t.note.replace("\n", " "), ""]
    if t.authored_turns:
        nums = ", ".join(str(n) for n in t.authored_turns)
        lines += [
            f"> Turn(s) {nums} are authored in `run_tests.py`, not quoted from "
            "`test-plan.md`, because the plan describes them without giving the text.",
            "",
        ]
    lines += ["---", ""]

    for x in result["exchanges"]:
        lines += [
            f"## Turn {x['n']} — input",
            "",
            "```",
            x["user"],
            "```",
            "",
            f"## Turn {x['n']} — reply ({len(x['assistant']):,} chars)",
            "",
            x["assistant"],
            "",
            "---",
            "",
        ]

    outdir.mkdir(parents=True, exist_ok=True)
    path = outdir / f"{t.id}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def print_summary(results: list[dict]) -> None:
    rows = []
    for r in results:
        t: TestCase = r["test"]
        per_turn = " / ".join(str(len(x["assistant"])) for x in r["exchanges"])
        rows.append((t.id, t.stage_file, str(len(t.turns)), str(r["total_chars"]), per_turn))

    headers = ("TEST", "STAGE FILE", "TURNS", "REPLY CHARS", "PER TURN")
    widths = [
        max(len(headers[i]), max((len(row[i]) for row in rows), default=0))
        for i in range(len(headers))
    ]

    def line(cells):
        return "  ".join(c.ljust(widths[i]) for i, c in enumerate(cells)).rstrip()

    print()
    print(line(headers))
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print(line(row))
    print()
    print(f"{len(results)} case(s). Transcripts in {RESULTS_DIR}")
    print("No pass/fail was computed. Read the transcripts against test-plan.md.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", help="comma separated test ids, e.g. T4,T7")
    ap.add_argument("--model", default=DEFAULT_MODEL, help=f"default: {DEFAULT_MODEL}")
    ap.add_argument("--outdir", default=str(RESULTS_DIR))
    ap.add_argument("--dry-run", action="store_true",
                    help="build the prompts and report sizes without calling the API")
    ap.add_argument("--sleep", type=float, default=0.0,
                    help="seconds to wait between cases")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    tests = TESTS
    if args.only:
        wanted = {s.strip().upper() for s in args.only.split(",") if s.strip()}
        tests = [t for t in TESTS if t.id.upper() in wanted]
        # allow --only T3 to select T3a and T3b
        if not tests:
            tests = [t for t in TESTS if any(t.id.upper().startswith(w) for w in wanted)]
        if not tests:
            raise SystemExit(f"No tests matched --only {args.only}")

    preflight(tests)

    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key and not args.dry_run:
        raise SystemExit("OPENAI_API_KEY is not set in the environment.")

    outdir = Path(args.outdir)
    print(f"Model: {args.model}   Temperature: {TEMPERATURE}"
          f"{'   (DRY RUN)' if args.dry_run else ''}")
    print(f"Running {len(tests)} case(s):")

    results = []
    for i, t in enumerate(tests):
        result = run_case(t, api_key, args.model, args.dry_run)
        path = write_result(result, outdir)
        print(f"    -> {path}")
        results.append(result)
        if args.sleep and i < len(tests) - 1 and not args.dry_run:
            time.sleep(args.sleep)

    print_summary(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
