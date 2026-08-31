#!/usr/bin/env python3
"""
Blind three-way model comparison for The Courtship Decoder.

Runs the same test cases as run_tests.py -- imported from it, so there is one
source of truth for the cases -- against OpenAI, Anthropic and Google, and
writes the replies as anonymous candidates:

    results/<test-id>/candidate-a.md
    results/<test-id>/candidate-b.md
    results/<test-id>/candidate-c.md

Which provider is A, B or C is shuffled independently for every test. The
mapping is written ONLY to results/_key.md. No provider or model name appears
in a candidate file, in its headings, or in its filename.

Rules carried over from run_tests.py and the test plan:
  - System prompt = full instructions file + delimiter + exactly ONE stage file.
  - Every test starts a fresh conversation, per provider. No shared history.
  - Temperature 1.
  - No pass/fail judgement in code. Read the transcripts.

Keys are read from .env next to this script:
    OPENAI_API_KEY=sk-...
    ANTHROPIC_API_KEY=sk-ant-...
    GOOGLE_API_KEY=...

Usage:
    python compare_models.py --list-models        # see what each key can reach
    python compare_models.py --list-models --all-models

    python compare_models.py \
        --openai-model <id> --anthropic-model <id> --google-model <id>

    ... --only T4,T7        run a subset
    ... --dry-run           build prompts, no API calls
    ... --seed 42           reproduce a previous shuffle
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# The cases, the prompt builder and the delimiter all come from the existing
# harness so the two scripts can never drift apart.
from run_tests import (
    HERE,
    INSTRUCTIONS_FILE,
    TEMPERATURE,
    TESTS,
    TestCase,
    build_system_prompt,
    preflight,
)

ENV_FILE = HERE / ".env"
RESULTS_DIR = HERE / "results"
KEY_FILE = "_key.md"

REQUEST_TIMEOUT = 300
MAX_RETRIES = 4
MAX_OUTPUT_TOKENS = 8192  # Anthropic requires this; Google is capped for parity.

LETTERS = ["a", "b", "c"]


# ---------------------------------------------------------------------------
# .env
# ---------------------------------------------------------------------------

def load_env() -> dict:
    if not ENV_FILE.is_file():
        raise SystemExit(f"No .env file found at {ENV_FILE}")
    out = {}
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class ApiError(RuntimeError):
    pass


def http_json(url: str, headers: dict, payload: dict | None = None,
              method: str = "POST") -> dict:
    """One request, retried on transient failures. Returns the decoded body."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    hdrs = dict(headers)
    if data is not None:
        hdrs.setdefault("Content-Type", "application/json")

    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:600]
            last_err = f"HTTP {e.code}: {detail}"
            # Quota exhaustion is permanent -- do not sit through the backoff.
            permanent_429 = e.code == 429 and any(
                s in detail for s in
                ("insufficient_quota", "credit_balance_exhausted", "billing")
            )
            retryable = (not permanent_429) and (
                e.code in (408, 409, 429, 529) or e.code >= 500
            )
            if retryable and attempt < MAX_RETRIES:
                wait = 2 ** attempt
                print(f"      retry {attempt}/{MAX_RETRIES} after {wait}s ({e.code})")
                time.sleep(wait)
                continue
            raise ApiError(last_err)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last_err = str(e)
            if attempt < MAX_RETRIES:
                wait = 2 ** attempt
                print(f"      retry {attempt}/{MAX_RETRIES} after {wait}s ({last_err})")
                time.sleep(wait)
                continue
            raise ApiError(last_err)

    raise ApiError(last_err or "unknown error")


# ---------------------------------------------------------------------------
# Providers
#
# Every adapter exposes the same two methods:
#     list_models()            -> list[str]
#     call(system, messages)   -> str
# where `messages` is [{"role": "user"|"assistant", "content": str}, ...].
# The runner below never touches anything else, so it stays provider agnostic.
# ---------------------------------------------------------------------------

class Provider:
    name = ""
    env_key = ""
    # Substrings that would give the provider away if they turned up in a
    # candidate file. Used only to warn, never to edit a reply.
    tells: tuple[str, ...] = ()

    def __init__(self, api_key: str, model: str | None = None):
        self.api_key = api_key
        self.model = model

    def list_models(self) -> list[str]:
        raise NotImplementedError

    def call(self, system: str, messages: list[dict]) -> str:
        raise NotImplementedError


class OpenAIProvider(Provider):
    name = "OpenAI"
    env_key = "OPENAI_API_KEY"
    tells = ("openai", "chatgpt", "gpt-", "gpt4", "gpt 4")

    BASE = "https://api.openai.com/v1"

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"}

    def list_models(self) -> list[str]:
        body = http_json(f"{self.BASE}/models", self._headers(), method="GET")
        return sorted(m["id"] for m in body.get("data", []))

    def call(self, system: str, messages: list[dict]) -> str:
        payload = {
            "model": self.model,
            "temperature": TEMPERATURE,
            "messages": [{"role": "system", "content": system}] + messages,
        }
        body = http_json(f"{self.BASE}/chat/completions", self._headers(), payload)
        try:
            return body["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError):
            raise ApiError(f"unexpected response shape: {json.dumps(body)[:400]}")


class AnthropicProvider(Provider):
    name = "Anthropic"
    env_key = "ANTHROPIC_API_KEY"
    tells = ("anthropic", "claude")

    BASE = "https://api.anthropic.com/v1"
    VERSION = "2023-06-01"

    def _headers(self) -> dict:
        return {"x-api-key": self.api_key, "anthropic-version": self.VERSION}

    def list_models(self) -> list[str]:
        out, params = [], {"limit": 100}
        while True:
            url = f"{self.BASE}/models?" + urllib.parse.urlencode(params)
            body = http_json(url, self._headers(), method="GET")
            out += [m["id"] for m in body.get("data", [])]
            if body.get("has_more") and body.get("last_id"):
                params["after_id"] = body["last_id"]
                continue
            break
        return sorted(out)

    def call(self, system: str, messages: list[dict]) -> str:
        # The system prompt is a top level field here, never a message.
        payload = {
            "model": self.model,
            "max_tokens": MAX_OUTPUT_TOKENS,
            "temperature": TEMPERATURE,
            "system": system,
            "messages": [
                {"role": m["role"], "content": m["content"]} for m in messages
            ],
        }
        body = http_json(f"{self.BASE}/messages", self._headers(), payload)
        blocks = body.get("content") or []
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        if not text:
            raise ApiError(
                f"empty reply (stop_reason={body.get('stop_reason')}): "
                f"{json.dumps(body)[:400]}"
            )
        return text


class GoogleProvider(Provider):
    name = "Google"
    env_key = "GOOGLE_API_KEY"
    tells = ("google", "gemini", "bard", "deepmind")

    BASE = "https://generativelanguage.googleapis.com/v1beta"

    def _url(self, path: str, **params) -> str:
        params["key"] = self.api_key
        return f"{self.BASE}/{path}?" + urllib.parse.urlencode(params)

    def list_models(self) -> list[str]:
        out, token = [], None
        while True:
            params = {"pageSize": 200}
            if token:
                params["pageToken"] = token
            body = http_json(self._url("models", **params), {}, method="GET")
            for m in body.get("models", []):
                methods = m.get("supportedGenerationMethods", [])
                if not methods or "generateContent" in methods:
                    out.append(m["name"].removeprefix("models/"))
            token = body.get("nextPageToken")
            if not token:
                break
        return sorted(set(out))

    def call(self, system: str, messages: list[dict]) -> str:
        payload = {
            "system_instruction": {"parts": [{"text": system}]},
            "contents": [
                {
                    "role": "model" if m["role"] == "assistant" else "user",
                    "parts": [{"text": m["content"]}],
                }
                for m in messages
            ],
            "generationConfig": {
                "temperature": TEMPERATURE,
                "maxOutputTokens": MAX_OUTPUT_TOKENS,
            },
        }
        model = self.model.removeprefix("models/")
        body = http_json(self._url(f"models/{model}:generateContent"), {}, payload)

        candidates = body.get("candidates") or []
        if not candidates:
            fb = body.get("promptFeedback", {})
            raise ApiError(f"no candidates returned (promptFeedback={fb})")
        cand = candidates[0]
        parts = (cand.get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if "text" in p)
        if not text:
            raise ApiError(
                f"empty reply (finishReason={cand.get('finishReason')}): "
                f"{json.dumps(body)[:400]}"
            )
        return text


PROVIDER_CLASSES = [OpenAIProvider, AnthropicProvider, GoogleProvider]
PROVIDER_BY_NAME = {c.name.lower(): c for c in PROVIDER_CLASSES}


# ---------------------------------------------------------------------------
# --list-models
# ---------------------------------------------------------------------------

# Non-chat endpoints clutter the OpenAI list. Hidden unless --all-models.
NON_CHAT = re.compile(
    r"(embedding|whisper|tts|dall-e|moderation|audio|image|transcribe|"
    r"realtime|davinci|babbage|aqa|veo|imagen|learnlm)",
    re.I,
)


def list_models(env: dict, show_all: bool) -> int:
    failures = 0
    for cls in PROVIDER_CLASSES:
        key = env.get(cls.env_key, "").strip()
        print(f"\n{cls.name}")
        print("-" * len(cls.name))
        if not key:
            print(f"  (no {cls.env_key} in .env -- skipped)")
            failures += 1
            continue
        try:
            ids = cls(key).list_models()
        except ApiError as e:
            print(f"  FAILED: {e}")
            failures += 1
            continue

        shown = ids if show_all else [i for i in ids if not NON_CHAT.search(i)]
        for i in shown:
            print(f"  {i}")
        hidden = len(ids) - len(shown)
        print(f"  ({len(shown)} shown"
              + (f", {hidden} non-chat hidden -- use --all-models" if hidden else "")
              + ")")

    print("\nPick one flagship per provider, then run:")
    print("  python compare_models.py --openai-model <id> "
          "--anthropic-model <id> --google-model <id>")
    return 1 if failures else 0


# ---------------------------------------------------------------------------
# Running a case
# ---------------------------------------------------------------------------

def run_case(test: TestCase, provider: Provider, dry_run: bool) -> dict:
    """One fresh conversation for this test against this provider."""
    system_prompt = build_system_prompt(test.stage_file)
    history: list[dict] = []
    exchanges = []
    started = time.time()

    for i, turn in enumerate(test.turns, start=1):
        history.append({"role": "user", "content": turn})
        if dry_run:
            reply = "(dry run -- no API call made)"
        else:
            print(f"      turn {i}...")
            reply = provider.call(system_prompt, history)
            history.append({"role": "assistant", "content": reply})
        exchanges.append({"n": i, "user": turn, "assistant": reply})

    return {
        "provider": provider.name,
        "model": provider.model,
        "system_prompt_chars": len(system_prompt),
        "exchanges": exchanges,
        "total_chars": sum(len(x["assistant"]) for x in exchanges),
        "seconds": round(time.time() - started, 1),
    }


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def candidate_markdown(test: TestCase, letter: str, result: dict) -> str:
    """The blind transcript. Deliberately says nothing about who produced it."""
    lines = [
        f"# {test.id} — candidate {letter.upper()}",
        "",
        f"- Plan case: {test.plan_ref}",
        f"- Stage file attached: `{test.stage_file}` (the only one)",
        f"- System prompt: `{INSTRUCTIONS_FILE}` + delimiter + `{test.stage_file}` "
        f"({result['system_prompt_chars']:,} chars)",
        f"- Temperature: {TEMPERATURE}",
        f"- Conversation: fresh, {len(test.turns)} user turn(s), no prior history",
        "",
        "> Blind sample. The candidate letter is shuffled per test; the mapping "
        f"back to a model lives only in `{KEY_FILE}`.",
        "",
    ]
    if test.note:
        lines += ["> " + test.note.replace("\n", " "), ""]
    if test.authored_turns:
        nums = ", ".join(str(n) for n in test.authored_turns)
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
    return "\n".join(lines)


def check_for_tells(text: str, providers: dict) -> list[str]:
    """Warn if a reply names its own maker. Never edits the reply."""
    low = text.lower()
    return [
        f"{p.name}: '{t}'"
        for p in providers.values() for t in p.tells if t in low
    ]


def write_key(outdir: Path, assignments: dict, providers: dict, seed: int,
              errors: list[str], leaks: list[str], dry_run: bool) -> Path:
    lines = [
        "# Key — which candidate is which model",
        "",
        "**Do not open this until the transcripts have been read.**",
        "",
        f"- Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        f"- Shuffle seed: `{seed}` (reproduce with `--seed {seed}`)",
        f"- Temperature: {TEMPERATURE}",
    ]
    if dry_run:
        lines.append("- **DRY RUN** -- no API calls were made.")
    lines += ["", "## Models under test", ""]
    lines += ["| Provider | Model |", "| --- | --- |"]
    for p in providers.values():
        lines.append(f"| {p.name} | `{p.model}` |")

    lines += [
        "",
        "## Letter assignment per test",
        "",
        "Shuffled independently for every test, so a letter means nothing "
        "across rows.",
        "",
        "| Test | A | B | C |",
        "| --- | --- | --- | --- |",
    ]
    for test_id, mapping in assignments.items():
        cells = [mapping.get(l, "—") for l in LETTERS]
        lines.append(f"| {test_id} | {cells[0]} | {cells[1]} | {cells[2]} |")

    if leaks:
        lines += ["", "## Self-identification warnings", "",
                  "A reply contained a word that names its own maker. The reply "
                  "was left exactly as returned; the blinding is compromised for "
                  "these files.", ""]
        lines += [f"- {l}" for l in leaks]

    if errors:
        lines += ["", "## Errors", ""] + [f"- {e}" for e in errors]

    lines += ["", "No pass/fail was computed. Read the candidates against "
              "`test-plan.md`.", ""]

    outdir.mkdir(parents=True, exist_ok=True)
    path = outdir / KEY_FILE
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--list-models", action="store_true",
                    help="print the model ids each key can reach, then exit")
    ap.add_argument("--all-models", action="store_true",
                    help="with --list-models, include non-chat models")
    ap.add_argument("--openai-model")
    ap.add_argument("--anthropic-model")
    ap.add_argument("--google-model")
    ap.add_argument("--only", help="comma separated test ids, e.g. T4,T7")
    ap.add_argument("--providers", default="openai,anthropic,google",
                    help="comma separated subset to run")
    ap.add_argument("--outdir", default=str(RESULTS_DIR))
    ap.add_argument("--seed", type=int,
                    help="fix the shuffle so a run can be reproduced")
    ap.add_argument("--dry-run", action="store_true",
                    help="build the prompts and report sizes without calling any API")
    ap.add_argument("--sleep", type=float, default=0.0,
                    help="seconds to wait between cases")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    env = load_env()

    if args.list_models:
        return list_models(env, args.all_models)

    # --- providers ---------------------------------------------------------
    wanted = [p.strip().lower() for p in args.providers.split(",") if p.strip()]
    models = {
        "openai": args.openai_model,
        "anthropic": args.anthropic_model,
        "google": args.google_model,
    }
    providers: dict[str, Provider] = {}
    for name in wanted:
        cls = PROVIDER_BY_NAME.get(name)
        if not cls:
            raise SystemExit(f"Unknown provider: {name}")
        if not models[name]:
            raise SystemExit(
                f"No model given for {cls.name}. Run --list-models first, then "
                f"pass --{name}-model <id>."
            )
        key = env.get(cls.env_key, "").strip()
        if not key and not args.dry_run:
            raise SystemExit(f"{cls.env_key} is not set in {ENV_FILE}")
        providers[name] = cls(key, models[name])

    if len(providers) > len(LETTERS):
        raise SystemExit(f"Only {len(LETTERS)} candidate letters are defined.")

    # --- tests -------------------------------------------------------------
    tests = TESTS
    if args.only:
        want = {s.strip().upper() for s in args.only.split(",") if s.strip()}
        tests = [t for t in TESTS if t.id.upper() in want]
        if not tests:  # allow --only T3 to select T3a and T3b
            tests = [t for t in TESTS
                     if any(t.id.upper().startswith(w) for w in want)]
        if not tests:
            raise SystemExit(f"No tests matched --only {args.only}")

    preflight(tests)

    seed = args.seed if args.seed is not None else random.randrange(1, 10 ** 6)
    rng = random.Random(seed)
    outdir = Path(args.outdir)

    print(f"Providers: {len(providers)}   Temperature: {TEMPERATURE}   "
          f"Seed: {seed}{'   (DRY RUN)' if args.dry_run else ''}")
    print(f"Running {len(tests)} case(s) x {len(providers)} provider(s):")

    assignments: dict[str, dict[str, str]] = {}
    errors: list[str] = []
    leaks: list[str] = []
    summary_rows = []

    for i, test in enumerate(tests):
        print(f"\n  {test.id}  [{test.stage_file}]  {len(test.turns)} turn(s)")

        # Shuffle the letters for this test only.
        order = list(providers)
        rng.shuffle(order)
        letters = dict(zip(LETTERS, order))
        assignments[test.id] = {l: providers[n].name for l, n in letters.items()}

        case_dir = outdir / test.id
        case_dir.mkdir(parents=True, exist_ok=True)

        for letter, pname in letters.items():
            provider = providers[pname]
            print(f"    candidate {letter.upper()}")
            try:
                result = run_case(test, provider, args.dry_run)
            except ApiError as e:
                msg = f"{test.id} / candidate {letter.upper()} ({provider.name}): {e}"
                print(f"      ERROR: {e}")
                errors.append(msg)
                summary_rows.append((test.id, letter.upper(), "ERROR", "-"))
                continue

            text = candidate_markdown(test, letter, result)
            for tell in check_for_tells(
                " ".join(x["assistant"] for x in result["exchanges"]), providers
            ):
                leaks.append(f"{test.id} / candidate {letter.upper()} mentions {tell}")
                print(f"      WARNING: reply mentions {tell}")

            path = case_dir / f"candidate-{letter}.md"
            path.write_text(text, encoding="utf-8")
            print(f"      -> {path.relative_to(HERE)}  "
                  f"({result['total_chars']:,} chars, {result['seconds']}s)")
            summary_rows.append((test.id, letter.upper(),
                                 f"{result['total_chars']:,}",
                                 f"{result['seconds']}s"))

        if args.sleep and i < len(tests) - 1 and not args.dry_run:
            time.sleep(args.sleep)

    key_path = write_key(outdir, assignments, providers, seed, errors, leaks,
                         args.dry_run)

    # --- summary -----------------------------------------------------------
    headers = ("TEST", "CAND", "REPLY CHARS", "TIME")
    widths = [max(len(headers[i]), max((len(r[i]) for r in summary_rows), default=0))
              for i in range(len(headers))]

    def line(cells):
        return "  ".join(str(c).ljust(widths[i]) for i, c in enumerate(cells)).rstrip()

    print()
    print(line(headers))
    print("  ".join("-" * w for w in widths))
    for row in summary_rows:
        print(line(row))

    print(f"\n{len(tests)} case(s) x {len(providers)} provider(s). "
          f"Candidates in {outdir}")
    print(f"Mapping written to {key_path.relative_to(HERE)} -- read the "
          "transcripts before you open it.")
    if leaks:
        print(f"{len(leaks)} self-identification warning(s). See {KEY_FILE}.")
    if errors:
        print(f"{len(errors)} error(s). See {KEY_FILE}.")
    print("No pass/fail was computed. Read the candidates against test-plan.md.")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
