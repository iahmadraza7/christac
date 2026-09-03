"""
Tests for the parts that cost money or control access. No API calls: the
provider is replaced with a fake that returns whatever the test wants.

Run:  python -m server.test_server
"""
from __future__ import annotations

import os
import sys

# Configure before importing the app: it reads its settings at import time.
import tempfile

# The spend ledger is a real file; give the tests a throwaway one.
LEDGER_PATH = os.path.join(tempfile.mkdtemp(), "spend.json")

os.environ.update({
    "PROVIDER": "anthropic",
    "MODEL": "test-model",
    "ANTHROPIC_API_KEY": "test-key",
    "ALLOWED_ORIGINS": "https://her-site.kajabi.com,https://www.her-site.com",
    "RATE_LIMIT_PER_MINUTE": "5",
    "MAX_TOKENS_PER_CONVERSATION": "1000",
    "CONVERSATION_TTL_MINUTES": "60",
    "VERDICT_MATCH_THRESHOLD": "0.35",
    "MAX_TURNS_PER_CONVERSATION": "40",
    "REPEAT_DECODE_WINDOW_TURNS": "3",
    "REPEAT_DECODE_SIMILARITY": "0.60",
    "MONTHLY_SPEND_CEILING_USD": "1.00",
    "SPEND_LEDGER_PATH": LEDGER_PATH,
})


from fastapi.testclient import TestClient  # noqa: E402

from . import app as A  # noqa: E402
from . import prompt as P  # noqa: E402
import pathlib  # noqa: E402
from . import cost  # noqa: E402
from . import providers  # noqa: E402

ORIGIN = "https://her-site.kajabi.com"
CALLS: list[dict] = []
NEXT_REPLY = {"text": "Tell me more."}


class FakeProvider(providers.Provider):
    name = "fake"

    async def call(self, system, messages, cache_key):
        CALLS.append({"system": system, "messages": list(messages),
                      "cache_key": cache_key})
        return providers.Reply(text=NEXT_REPLY["text"], input_tokens=100,
                               output_tokens=20, cached_input_tokens=80)


A.PROVIDER = FakeProvider("k", "test-model", 1024)
client = TestClient(A.app)

PASSED, FAILED = 0, 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  pass  {name}")
    else:
        FAILED += 1
        print(f"  FAIL  {name}" + (f"  — {detail}" if detail else ""))


def say(text: str, cid: str | None = None, origin: str = ORIGIN):
    payload = {"message": text}
    if cid:
        payload["conversation_id"] = cid
    return client.post("/api/chat", json=payload, headers={"Origin": origin})


def fresh():
    CALLS.clear()
    A.STORE._data.clear()
    A.LIMITER._hits.clear()
    A.LEDGER._usd = 0.0
    NEXT_REPLY["text"] = "Tell me more."


# ---------------------------------------------------------------------------
print("\nThe lesson file is not loaded before a verdict")
fresh()
r = say("Stage 1. His bio is blank and there are photos of him with other women.")
check("request succeeds", r.status_code == 200, r.text[:200])
body = r.json()
check("no verdict yet", body["verdict_delivered"] is False)
check("lesson not loaded", body["lesson_loaded"] is False)
check("system prompt has no lesson part", CALLS[-1]["system"].lesson is None)
before_chars = CALLS[-1]["system"].chars
check("system prompt is instructions + one decode file only",
      before_chars < 60_000, f"{before_chars:,} chars")

print("\nThe lesson file is loaded once a verdict lands, and not before")
# The real Stage 1 dating-profile fail block, as the model would deliver it.
blocks = P.verdict_blocks(P.STAGE_1)
verdict_text = blocks[0]
NEXT_REPLY["text"] = verdict_text
r = say("Yes, all of that.", body["conversation_id"])
body2 = r.json()
check("verdict detected", body2["verdict_delivered"] is True,
      f"score={body2['verdict_score']}")
check("scored at or above the threshold", body2["verdict_score"] >= 0.35)
check("lesson still not loaded on the verdict turn itself",
      body2["lesson_loaded"] is False)

NEXT_REPLY["text"] = "Anything else about him?"
r = say("What do I do now?", body2["conversation_id"])
body3 = r.json()
check("lesson loaded on the next turn", body3["lesson_loaded"] is True)
after_chars = CALLS[-1]["system"].chars
check("system prompt grew by the lesson file",
      after_chars > before_chars + 80_000,
      f"{before_chars:,} -> {after_chars:,}")
check("verdict stays delivered", body3["verdict_delivered"] is True)

print("\nThe cacheable prefix is not disturbed when the lesson is appended")
check("prefix identical before and after the verdict",
      CALLS[0]["system"].prefix == CALLS[-1]["system"].prefix)
check("lesson is a suffix, never a prefix",
      CALLS[-1]["system"].text.startswith(CALLS[0]["system"].prefix))

print("\nNon-verdict replies never unlock the lesson")
fresh()
r = say("Stage 2. We've been on 2 dates.")
cid = r.json()["conversation_id"]
for reply in ["What stage are you in with this man?",
              "That's outside what I do here, beautiful. I decode his behavior "
              "so you can see what he's actually showing you.",
              "How many times have you been out?"]:
    NEXT_REPLY["text"] = reply
    out = say("ok", cid).json()
    check(f"no verdict for: {reply[:38]}…", out["verdict_delivered"] is False,
          f"score={out['verdict_score']}")

print("\nStage resolution picks exactly one decode file")
cases = [
    ("Stage 1. His profile says he's seeing where things go.", P.STAGE_1),
    ("Stage 2. We've been on 2 dates.", P.STAGE_2_P1),
    ("Stage 2. We've been on 6 dates.", P.STAGE_2_P2),
    ("Stage 2, phase 2, he still hasn't asked.", P.STAGE_2_P2),
    ("Stage 3. We had the no girlfriend standard conversation.", P.STAGE_3),
    ("Stage 4. He proposed in June.", P.STAGE_4),
    ("I need help with this guy.", P.STAGE_1),
    ("We've been on three dates and I'm in stage 2", P.STAGE_2_P2),
]
for text, expected in cases:
    got = P.resolve_stage(text, None)
    check(f"{text[:44]:46} -> {expected}", got == expected, f"got {got}")

check("stage is sticky once known",
      P.resolve_stage("he texted me again", P.STAGE_3) == P.STAGE_3)
check("a named stage overrides the sticky one",
      P.resolve_stage("actually stage 4 now", P.STAGE_3) == P.STAGE_4)

print("\nOnly one decode file is ever in the system prompt")
for stage in P.STAGES:
    text = P.build(stage, False).text
    others = [n for s, n in P.DECODE_FILE.items()
              if s != stage and f"KNOWLEDGE FILE: {n}" in text]
    check(f"{P.DECODE_FILE[stage]:34} alone", not others, f"also found {others}")

print("\nOrigin allowlist")
fresh()
check("allowed origin accepted", say("hi", origin=ORIGIN).status_code == 200)
check("second allowed origin accepted",
      say("hi", origin="https://www.her-site.com").status_code == 200)
bad = say("hi", origin="https://evil.example")
check("unknown origin refused", bad.status_code == 403, bad.text[:120])
check("refusal names the reason", bad.json()["code"] == "origin_not_allowed")
check("no CORS header for a refused origin",
      "access-control-allow-origin" not in bad.headers)
ok = say("hi", origin=ORIGIN)
check("CORS header echoes the allowed origin",
      ok.headers.get("access-control-allow-origin") == ORIGIN)

print("\nThe page can only be framed by the allowlist")
page = client.get("/")
csp = page.headers.get("content-security-policy", "")
check("page served", page.status_code == 200)
check("frame-ancestors names the Kajabi domain", ORIGIN in csp, csp[:120])
check("no X-Frame-Options to fight it", "x-frame-options" not in page.headers)

print("\nRate limit per IP")
fresh()
codes = [say("hi").status_code for _ in range(7)]
check("first 5 allowed", codes[:5] == [200] * 5, str(codes))
check("6th and 7th refused", codes[5:] == [429, 429], str(codes))
last = say("hi")
check("refusal carries Retry-After", "retry-after" in last.headers)

print("\nHard token cap per conversation")
fresh()
r = say("hello")
cid = r.json()["conversation_id"]
seen = None
for _ in range(12):
    A.LIMITER._hits.clear()   # testing the cap here, not the rate limit
    out = say("again", cid)
    if out.status_code == 409:
        seen = out.json()
        break
check("conversation is cut off at the cap", seen is not None)
if seen:
    check("cap refusal names the reason", seen["code"] == "conversation_limit")
conv = A.STORE.get(cid)
check("spend never exceeded the configured cap",
      conv is None or conv.tokens_used <= 1000 + 120,
      f"used {conv.tokens_used if conv else '?'}")

print("\nAnthropic marks the system prompt as cacheable")
captured = {}


async def fake_post(url, headers, payload):
    captured.update(payload)
    return {"content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": 10, "output_tokens": 5,
                      "cache_creation_input_tokens": 0,
                      "cache_read_input_tokens": 900}}


providers.Provider._post = staticmethod(fake_post)
import asyncio  # noqa: E402

sp = P.build(P.STAGE_1, True)
ap = providers.AnthropicProvider("k", "m", 1024)
reply = asyncio.run(ap.call(sp, [{"role": "user", "content": "hi"}],
                            "decoder:1:lesson"))
sysblocks = captured.get("system", [])
check("system is sent as blocks", isinstance(sysblocks, list) and len(sysblocks) == 2)
check("every block carries a cache breakpoint",
      all(b.get("cache_control", {}).get("type") == "ephemeral" for b in sysblocks))
check("the stable prefix is the first block",
      sysblocks and sysblocks[0]["text"] == sp.prefix)
check("cached tokens are reported back", reply.cached_input_tokens == 900)

print("\nA conversation cannot sprawl past the turn limit")
fresh()
A.MAX_TURNS_PER_CONVERSATION = 6   # lowered here only, to reach it quickly
r = say("Stage 1. Tell me about him.")
cid = r.json()["conversation_id"]
codes = []
for i in range(8):
    A.LIMITER._hits.clear()
    NEXT_REPLY["text"] = f"Reply {i} worded completely differently every single time."
    codes.append(say(f"An entirely unrelated new thing number {i}.", cid).status_code)
check("the turn limit stops it", 409 in codes, str(codes))
check("it stops at the configured turn, not before",
      codes.index(409) == 5 if 409 in codes else False,
      f"first refusal at extra turn {codes.index(409) + 1 if 409 in codes else None}")
conv = A.STORE.get(cid)
check("no turn beyond the limit reached the model",
      conv is None or conv.turns <= 6, f"turns={conv.turns if conv else '?'}")

A.MAX_TURNS_PER_CONVERSATION = 40

print("\nGoing back over the same man does not call the model again")
fresh()
SCENARIO = ("Stage 1. His bio is blank and there are photos of him with other "
            "women and he says he is seeing where things go.")
cid = say(SCENARIO).json()["conversation_id"]
NEXT_REPLY["text"] = P.verdict_blocks(P.STAGE_1)[0]
out = say("Yes that is him.", cid).json()
check("a verdict landed first", out["verdict_delivered"] is True)
before = len(CALLS)
again = say(SCENARIO, cid).json()
check("the repeat was answered with no API call at all", len(CALLS) == before,
      f"{len(CALLS) - before} extra call(s)")
check("it is flagged as a blocked repeat",
      again.get("repeat_decode_blocked") is True)
check("the reply is the configured line", again["reply"] == A.REPEAT_REPLY)
check("no lesson file was attached to it", again["lesson_loaded"] is False)

print("\nBut pushback and a new man still reach the model")
before = len(CALLS)
NEXT_REPLY["text"] = "His behavior is still showing you the same thing."
push = say("But he has been really busy with work and he did say he sees a future "
           "with me, and I have never felt like this before.", cid).json()
check("pushback is not treated as a repeat",
      len(CALLS) == before + 1 and not push.get("repeat_decode_blocked"))
before = len(CALLS)
NEXT_REPLY["text"] = "What stage are you in with this man, Queen?"
newman = say("I want to decode another man now.", cid).json()
check("a new man is not treated as a repeat",
      len(CALLS) == before + 1 and not newman.get("repeat_decode_blocked"))

print("\nThe monthly ceiling stops the service rather than letting the bill run")
fresh()
check("serving while under the ceiling",
      client.get("/api/health").json()["serving"] is True)
A.LEDGER.add(0.99)
check("still serving just under the ceiling", say("hello").status_code == 200)
A.LEDGER.add(0.02)
blocked = say("hello")
check("refused once the ceiling is reached", blocked.status_code == 503,
      str(blocked.status_code))
check("the refusal names the reason",
      blocked.json()["code"] == "monthly_ceiling_reached")
check("health reports it has stopped serving",
      client.get("/api/health").json()["serving"] is False)
before = len(CALLS)
say("hello")
check("nothing reaches the model once the ceiling is reached",
      len(CALLS) == before)

print("\nThe spend ledger survives a restart")
A.LEDGER._usd = 0.0
A.LEDGER.add(0.25)
reloaded = cost.MonthlyLedger(pathlib.Path(LEDGER_PATH), 1.00)
check("spend is read back from disk", abs(reloaded.spent() - 0.25) < 1e-9,
      str(reloaded.spent()))
check("a month with no ledger yet starts at zero",
      cost.MonthlyLedger(pathlib.Path(LEDGER_PATH + ".missing"), 1.0).spent() == 0.0)

print("\nCost arithmetic matches the published rates")
rates = cost.Rates(input_per_mtok=5.0, output_per_mtok=25.0,
                   cache_write_per_mtok=6.25, cache_read_per_mtok=0.50)
check("1M fresh input costs $5.00", abs(rates.usd(1_000_000, 0, 0, 0) - 5.0) < 1e-9)
check("1M output costs $25.00", abs(rates.usd(0, 0, 0, 1_000_000) - 25.0) < 1e-9)
check("1M cache write costs $6.25", abs(rates.usd(0, 1_000_000, 0, 0) - 6.25) < 1e-9)
check("1M cache read costs $0.50", abs(rates.usd(0, 0, 1_000_000, 0) - 0.50) < 1e-9)
rep = providers.Reply(text="x", input_tokens=13623, output_tokens=92,
                      cached_input_tokens=13617, cache_write_tokens=0)
check("a cached turn separates fresh input from cached",
      rep.fresh_input_tokens == 6, str(rep.fresh_input_tokens))
check("a cached turn costs far less than the same turn uncached",
      rates.usd_for(rep) < rates.usd(rep.input_tokens, 0, 0, rep.output_tokens) / 5)


print("\nThe new-man phrases are recognised, and nothing else is")
for _text, _want in [
    ("Another man. Stage 2, we have been on 6 dates.", True),
    ("I want to decode a different man.", True),
    ("Let's do another man.", True),
    ("Tell me about someone else.", True),
    ("Next guy.", True),
    ("But he has been really busy with work.", False),
    ("He texts me every day but has not planned another date.", False),
    ("His bio is blank and there are photos with other women.", False),
]:
    check(f"{_want!s:5} <- {_text[:46]}", P.mentions_a_new_man(_text) is _want)
check("the pattern holds no stray control characters",
      not any(ord(c) < 32 for c in P._NEW_MAN.pattern))



print("\nThree men in one sitting all get decoded")
fresh()
A.MAX_TURNS_PER_CONVERSATION = 4   # deliberately tight, to prove it is per man
MEN = [
    ("Stage 1. His bio is blank and there are photos of him with other women.",
     P.STAGE_1),
    ("Another man. Stage 2, we have been on 6 dates and he still has not asked.",
     P.STAGE_2_P2),
    ("I want to decode a different man. Stage 3, I gave him the "
     "No-Girlfriend Standard five months ago.", P.STAGE_3),
]
decoded, seen_men = [], []
cid = None
for n, (msg, want_stage) in enumerate(MEN, start=1):
    NEXT_REPLY["text"] = P.verdict_blocks(want_stage)[0]
    out = say(msg, cid).json()
    cid = out["conversation_id"]
    # a second turn on the same man, so the session total will exceed the
    # per-man limit and prove the limit is not session-wide
    A.LIMITER._hits.clear()
    NEXT_REPLY["text"] = f"A follow-up about man {n}, worded differently."
    out2 = say(f"One more detail about him, number {n}.", cid).json()
    decoded.append(out["verdict_delivered"])
    seen_men.append(out["man_number"])
    check(f"man {n} was decoded, not refused", out["verdict_delivered"] is True,
          f"code={out.get('code')}")
    check(f"man {n} got the right stage file", out["stage"] == want_stage,
          f"got {out['stage']} wanted {want_stage}")
    check(f"man {n} could still be talked about after his verdict",
          out2.get("code") is None, str(out2.get("code")))
check("all three men were decoded in one session", all(decoded))
check("the service counted them as three separate men", seen_men == [1, 2, 3],
      str(seen_men))
conv = A.STORE.get(cid)
check("the session ran past the per-man limit without being cut off",
      conv.turns > A.MAX_TURNS_PER_CONVERSATION,
      f"{conv.turns} turns, limit {A.MAX_TURNS_PER_CONVERSATION} per man")

print("\nMoving to a new man clears the last man's verdict and stage")
fresh()
A.MAX_TURNS_PER_CONVERSATION = 4
NEXT_REPLY["text"] = P.verdict_blocks(P.STAGE_3)[0]
first = say("Stage 3. I gave him the No-Girlfriend Standard five months ago.").json()
cid = first["conversation_id"]
check("the first man reached a verdict", first["verdict_delivered"] is True)
check("his stage was Stage 3", first["stage"] == P.STAGE_3)
NEXT_REPLY["text"] = "What stage are you in with this man, Queen?"
nxt = say("Let's do another man.", cid).json()
check("the move to a new man is reported", nxt["started_new_man"] is True)
check("the previous verdict no longer applies", nxt["verdict_delivered"] is False)
check("no lesson file is carried over", nxt["lesson_loaded"] is False)
check("the stage is re-identified, not inherited", nxt["stage"] == P.STAGE_1,
      f"got {nxt['stage']}")
check("his turn count started again", nxt["turns_this_man"] == 1,
      str(nxt["turns_this_man"]))

print("\nOne man still cannot be re-litigated past the limit")
fresh()
A.MAX_TURNS_PER_CONVERSATION = 4
cid = say("Stage 1. Tell me about this man of mine.").json()["conversation_id"]
codes = []
for i in range(6):
    A.LIMITER._hits.clear()
    NEXT_REPLY["text"] = f"Another differently worded reply, number {i}."
    codes.append(say(f"A completely separate new detail about him, number {i}.",
                     cid).status_code)
check("the same man is eventually cut off", 409 in codes, str(codes))
refusal = say("And one more thing about him.", cid)
check("the refusal names the per-man limit",
      refusal.json()["code"] == "man_turn_limit", refusal.json().get("code"))
check("but she can still move to another man afterwards",
      say("I want to decode another man.", cid).status_code == 200)
A.MAX_TURNS_PER_CONVERSATION = 40


print("\nHer positioning names reach the prompt before a verdict")
for st in P.STAGES:
    names = P.positioning_names(st)
    check(f"stage {st}: five names extracted", len(names) == 5, str(names))
    lesson_text = P.compose_lessons(st)
    check(f"stage {st}: every name is hers, found in her lesson text",
          all(n in lesson_text for n in names),
          str([n for n in names if n not in lesson_text]))

pre = P.build(P.STAGE_1, False)
check("the names are in the always-loaded part, so they land with the verdict",
      "HE LEADS ALL FORWARD MOVEMENT" in pre.prefix)
check("no lesson body is attached before a verdict", pre.lesson is None)
check("the block carries names only, no teaching",
      len(P.positioning_block(P.STAGE_1)) < 1200,
      str(len(P.positioning_block(P.STAGE_1))))

# The whole point of reading them per stage rather than hardcoding.
s1 = P.positioning_names(P.STAGE_1)
s3 = P.positioning_names(P.STAGE_3)
s4 = P.positioning_names(P.STAGE_4)
check("stage 3 words its fourth positioning differently from stage 1",
      s1[3] != s3[3], f"{s1[3]!r} vs {s3[3]!r}")
check("stage 4 words its third positioning differently from stage 1",
      s1[2] != s4[2], f"{s1[2]!r} vs {s4[2]!r}")
check("stage 3 keeps its own wording for the exclusivity slot",
      "COLLAPSING INTO THE GF STAGE" in s3[3], s3[3])
check("stage 4 keeps its own wording for the vetting slot",
      "FINAL VETTING STAGE" in s4[2], s4[2])

check("a run-on heading is cut at the name, not the teaching",
      P._positioning_name(
          "ACCESS IS EARNED Access includes everything: dates, texting, phone "
          "calls, relationship-style communication") == "ACCESS IS EARNED")
check("a trailing dash is trimmed",
      P._positioning_name(
          "THERE IS NO EXCLUSIVITY UNTIL ENGAGEMENT- You date multiple men.")
      == "THERE IS NO EXCLUSIVITY UNTIL ENGAGEMENT")
check("a short title-case heading is taken whole",
      P._positioning_name("No Intimacy Until the Vetting Process Is Complete")
      == "No Intimacy Until the Vetting Process Is Complete")


print(f"\n{PASSED} passed, {FAILED} failed")
sys.exit(1 if FAILED else 0)
