"""
Tests for the parts that cost money or control access. No API calls: the
provider is replaced with a fake that returns whatever the test wants.

Run:  python -m server.test_server
"""
from __future__ import annotations

import os
import sys
import json
import shutil
import subprocess

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
    "MAX_TOKENS_PER_CONVERSATION": "1500000",
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
        # Bill in proportion to what is actually sent. A flat number here is
        # why the per-man token bug went unnoticed: every turn looked cheap,
        # so no test ever felt the lesson file being re-sent each turn.
        sent = system.chars + sum(len(m["content"]) for m in messages)
        return providers.Reply(text=NEXT_REPLY["text"],
                               input_tokens=int(sent / 3.111),
                               output_tokens=max(1, len(NEXT_REPLY["text"]) // 4),
                               cached_input_tokens=0)


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

print("\nHard token cap per man")
fresh()
_real_cap = A.MAX_TOKENS_PER_CONVERSATION
A.MAX_TOKENS_PER_CONVERSATION = 40_000   # lowered here only, to reach it fast
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
check("no turn was bought once the cap was reached",
      conv is None or conv.tokens_this_man <= 40_000 + 60_000,
      f"used {conv.tokens_this_man if conv else '?'}")
A.MAX_TOKENS_PER_CONVERSATION = _real_cap

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


print("\nA setting can hold more than one line")
# Her approved reply is two sentences with a blank line between them. It has to
# survive a round trip through .env, where a value is one line.
shipped = A.load_env(pathlib.Path(".env.example")).get("REPEAT_DECODE_REPLY", "")
check("the shipped example keeps her first sentence",
      "We already read him, Queen." in shipped, repr(shipped))
check("the shipped example keeps her second sentence",
      "Want to decode another man?" in shipped, repr(shipped))
check("the shipped example turns the written break into a real newline",
      "\n\n" in shipped, repr(shipped))
check("no written-out escape is left in the loaded value",
      "\\n" not in shipped, repr(shipped))
check("the shipped example matches the wording built into the code",
      shipped == A.REPEAT_REPLY, f"{shipped!r} vs {A.REPEAT_REPLY!r}")

_env_dir = tempfile.mkdtemp()
_one_line = pathlib.Path(_env_dir) / "one-line.env"
_one_line.write_text("REPEAT_DECODE_REPLY=first line.\\n\\nsecond line.\n",
                     encoding="utf-8")
check("a written break loads as a real newline",
      A.load_env(_one_line)["REPEAT_DECODE_REPLY"] == "first line.\n\nsecond line.",
      repr(A.load_env(_one_line)["REPEAT_DECODE_REPLY"]))

# The bug this replaced: a value written across three lines lost everything
# after the first, because a continuation line has no "=" and is skipped.
_spanning = pathlib.Path(_env_dir) / "spanning.env"
_spanning.write_text("REPEAT_DECODE_REPLY=first line.\n\nsecond line.\n", encoding="utf-8")
check("a value spread over several lines still only reads the first, "
      "which is why the escape is needed",
      A.load_env(_spanning)["REPEAT_DECODE_REPLY"] == "first line.")

# A Windows path must not be mangled by the same rule.
_win = pathlib.Path(_env_dir) / "win.env"
_win.write_text("A_WINDOWS_PATH=C:" + chr(92) + "data" + chr(92) + "spend.json"
                + chr(10), encoding="utf-8")
check("a backslash that is not an escape is left alone",
      A.load_env(_win)["A_WINDOWS_PATH"]
      == "C:" + chr(92) + "data" + chr(92) + "spend.json",
      repr(A.load_env(_win).get("A_WINDOWS_PATH")))


print("\nA normal conversation never reaches the token cap")
# This is the bug she hit live: once a verdict lands every turn carries the
# lesson file, and the old 250,000 cap was gone after four or five of them.
fresh()
cid = say("Stage 1. His bio is blank and there are photos of him with other "
          "women.").json()["conversation_id"]
NEXT_REPLY["text"] = P.verdict_blocks(P.STAGE_1)[0]
after_verdict = say("Yes, that is him.", cid).json()
check("a verdict landed, so the lesson now rides on every turn",
      after_verdict["verdict_delivered"] is True)

codes = []
for i in range(8):
    A.LIMITER._hits.clear()
    A.LEDGER._usd = 0.0                 # the ceiling is not what is under test
    NEXT_REPLY["text"] = f"Follow up number {i} in her voice, worded freshly."
    codes.append(say(f"Tell me more about the {i} thing he did.", cid).status_code)
check("eight follow-up turns after the verdict all go through",
      codes == [200] * 8, str(codes))

conv = A.STORE.get(cid)
check("the lesson really was being carried each turn",
      CALLS[-1]["system"].lesson is not None)
per_turn = CALLS[-1]["system"].chars / 3.111
check("each of those turns is genuinely expensive",
      per_turn > 40_000, f"{per_turn:,.0f} tokens a turn")
check("and the whole thing still sits well under the cap",
      conv.tokens_this_man < A.MAX_TOKENS_PER_CONVERSATION,
      f"{conv.tokens_this_man:,} of {A.MAX_TOKENS_PER_CONVERSATION:,}")
check("the old 250,000 would have cut this conversation off",
      conv.tokens_this_man > 250_000,
      f"only reached {conv.tokens_this_man:,}, so this no longer proves the bug")

print("\nA new man starts with a clean token count")
before = conv.tokens_this_man
NEXT_REPLY["text"] = "What stage are you in with this man, Queen?"
A.LIMITER._hits.clear()
A.LEDGER._usd = 0.0
moved = say("I want to decode another man now.", cid).json()
conv = A.STORE.get(cid)
check("she was moved on to a second man", conv.man_number == 2, str(conv.man_number))
check("his spend did not follow her",
      conv.tokens_this_man < before,
      f"{conv.tokens_this_man:,} vs the first man's {before:,}")
check("what the page is told matches the man in play",
      moved["tokens_used"] == conv.tokens_this_man)
check("the session total still counts everything spent",
      conv.tokens_total > conv.tokens_this_man,
      f"total {conv.tokens_total:,}, this man {conv.tokens_this_man:,}")
check("the second man has his full budget",
      moved["tokens_remaining"] > A.MAX_TOKENS_PER_CONVERSATION - 200_000,
      f"{moved['tokens_remaining']:,} left")


print("\nA POST has to say where it came from")
fresh()
NEXT_REPLY["text"] = "Tell me more."


def post(headers):
    return client.post("/api/chat", json={"message": "hi"}, headers=headers)


# The hole: a bare script with no headers reached the model and spent her money.
before = len(CALLS)
naked = post({})
check("a request with no headers at all is refused",
      naked.status_code == 403, str(naked.status_code))
check("and it never reached the model", len(CALLS) == before)
check("the refusal names the reason",
      naked.json()["code"] == "origin_not_allowed")

bad = post({"Origin": "https://evil.example"})
check("a request from an unlisted site is still refused", bad.status_code == 403)

good = post({"Origin": ORIGIN})
check("the real page still works", good.status_code == 200, good.text[:120])

# Some browsers leave Origin off a same-origin POST. The page posts to a
# relative path, so its request is same-origin, and this is the fallback proof.
sfs = post({"Sec-Fetch-Site": "same-origin"})
check("a same-origin browser request with no Origin is accepted",
      sfs.status_code == 200, sfs.text[:120])

before = len(CALLS)
cross = post({"Sec-Fetch-Site": "cross-site"})
check("a cross-site request with no Origin is refused",
      cross.status_code == 403, str(cross.status_code))
check("and that one never reached the model either", len(CALLS) == before)

# Serving the page spends nothing, so it stays open.
page = client.get("/")
check("the page itself still loads with no Origin", page.status_code == 200)

print("\nThe escape hatch, for a browser that sends neither")
_was = A.REQUIRE_ORIGIN
A.REQUIRE_ORIGIN = False
loosened = post({})
check("turning REQUIRE_ORIGIN off lets a headerless POST through again",
      loosened.status_code == 200, str(loosened.status_code))
A.REQUIRE_ORIGIN = _was
check("and turning it back on closes it again", post({}).status_code == 403)


print("\nThe start command is declared in two places and they must agree")
# Railway changed builder from Nixpacks to Railpack, and Railpack did not read
# the start command out of railway.json - the deploy failed with "No start
# command detected". The Procfile is the portable answer, but it means two
# files now declare the same thing, so they can drift.
import json as _json

_railway = _json.loads(pathlib.Path("railway.json").read_text(encoding="utf-8"))
_rj_cmd = _railway["deploy"]["startCommand"]
_pf = pathlib.Path("Procfile").read_text(encoding="utf-8").strip()

check("the Procfile declares a web process", _pf.startswith("web: "), repr(_pf[:40]))
_pf_cmd = _pf[len("web: "):]
check("railway.json and the Procfile start the app the same way",
      _rj_cmd == _pf_cmd, f"{_rj_cmd!r} vs {_pf_cmd!r}")
check("the port comes from the host, not a hardcoded number",
      "$PORT" in _pf_cmd, _pf_cmd)
check("one worker only - the state, rate limiter and ledger are all in-process",
      "--workers 1" in _pf_cmd, _pf_cmd)
check("it points at the app this suite has been exercising",
      "server.app:app" in _pf_cmd, _pf_cmd)
check("the Procfile has no carriage returns, which some builders choke on",
      b"\r" not in pathlib.Path("Procfile").read_bytes())


print("\nThe chat page renders her emphasis (run in node, a real JS engine)")
# The renderer lives in chat.html and only a JS engine can judge it, so the
# real function is lifted out of the shipped page and exercised there rather
# than a copy of it. She was seeing the raw asterisks in "**...**".
_blocks = [b for _st in P.STAGES for b in P.verdict_blocks(_st)]
_bf = pathlib.Path(tempfile.mkdtemp()) / "blocks.json"
_bf.write_text(json.dumps(_blocks, ensure_ascii=False), encoding="utf-8")
_node = shutil.which("node")
if not _node:
    print("  SKIP  node is not installed here, so the page renderer was not exercised")
else:
    _r = subprocess.run(
        [_node, "server/test_render.js", "server/static/chat.html", str(_bf)],
        capture_output=True, text=True, encoding="utf-8")
    for _line in (_r.stdout or "").splitlines():
        _t = _line.strip()
        if _t.startswith(("pass", "FAIL", "CHANGED")):
            print("  " + _t)
    check("every check on the page renderer passes", _r.returncode == 0,
          ((_r.stdout or "") + (_r.stderr or ""))[-400:])


print("\nHer margin notes, and the placement each one got")
_INS = pathlib.Path("courtship-decoder-instructions.md").read_text(encoding="utf-8")
_DECODE = {st: pathlib.Path(P.DECODE_FILE[st]).read_text(encoding="utf-8")
           for st in P.STAGES}

# She has retired the ChatGPT build - "im only going to be using the website/new
# tool .. never the old" - so the 8,000 character instructions field is gone with
# it. The web app loads this file whole. What is left is a sanity bound: at the
# size of a decode file, something has been pasted in here by mistake.
_INS_SANITY_MAX = 20_000
check("the instructions are a set of rules, not a pasted decode file",
      len(_INS) < _INS_SANITY_MAX, f"{len(_INS):,} of {_INS_SANITY_MAX:,}")

# Cross-cutting: these must fire at any stage, and the instructions are the one
# part of the prompt that is loaded on every request whatever the stage.
check("item 2, the revised-verdict NOW, is in the instructions",
      "revises a verdict you already gave" in _INS and "NOW" in _INS)
check("item 3, an affirmative is not a new man unless one was just offered",
      '"Sure", "yes" or "okay" moves her to another man only when you just '
      'offered to decode one' in _INS)
check("item 5, join the written block on, is in the instructions",
      "join it on. Never let it start cold" in _INS)

# Stage specific: it names two stages, so by her own rule it left the
# instructions and went to those two decode files. That move is what paid for
# the three rules above.
_TEXTING = "In Stage 1 and Stage 2 Phase 1, inconsistent texting or calling says"
check("the stage-specific texting rule left the instructions",
      _TEXTING not in _INS)
check("and landed in Stage 1 and Stage 2 Phase 1",
      _TEXTING in _DECODE[P.STAGE_1] and _TEXTING in _DECODE[P.STAGE_2_P1])
check("and nowhere else",
      not any(_TEXTING in _DECODE[st] for st in
              (P.STAGE_2_P2, P.STAGE_3, P.STAGE_4)))

_OPENING = ("My Queen... this man's communication is already revealing that he "
            "may not be ready for marriage.")
_CLOSING = ("Right now, all that matters is whether he is eager and capable of "
            "taking you on a first date. That's it.")
_CLARITY = 'a woman finds herself asking "Where is this going?"'
_CAUTION = ("Your Majesty... nothing here tells me you need to walk away. But it "
            "does tell me to come back home to yourself.")

# Item 1. Her new fail response for the one filter it names.
_S1 = _DECODE[P.STAGE_1]
check("item 1: her new communication fail response is present, word for word",
      _S1.count(_OPENING) == 1 and _S1.count(_CLOSING) == 1)
check("item 1: it says 'may not be ready', the wording from this document",
      "may not be ready for marriage" in _S1
      and "could not be ready for marriage" not in _S1)
check("item 1: her two options survive intact",
      "1. Release the potential." in _S1 and "2. Stop investing and observe." in _S1)
check("item 1: the teaching note above it was left alone",
      "### Teaching Notes for Automatic Failing Signs — Stage 1, Marriage "
      "Readiness Filter: Communication Before the First Date" in _S1)
_early = [b for b in P.verdict_blocks(P.STAGE_1) if _OPENING in b]
check("item 1: it still registers as a verdict, so the lessons unlock",
      len(_early) == 1 and P.verdict_score(_early[0], P.STAGE_1) >= 0.9)

# Item 2. Her new caution response.
_S3 = _DECODE[P.STAGE_3]
check("item 2: her new caution response is present, word for word",
      _S3.count(_CAUTION) == 1)
check("item 2: her reworded phrases replaced the old ones",
      "how to get the ring" in _S3 and "how to get chosen" not in _S3
      and "instead of hoping" in _S3 and "instead of anticipating" not in _S3)
_caut = [b for b in P.verdict_blocks(P.STAGE_3) if _CAUTION in b]
check("item 2: it still registers as a verdict",
      len(_caut) == 1 and P.verdict_score(_caut[0], P.STAGE_3) >= 0.9)

# Item 3. Gone from every response, in both the plain and the bold form.
for _st in P.STAGES:
    check(f"item 3: stage {_st} has no 'Don't change a thing'",
          "change a thing" not in _DECODE[_st].lower())
check("item 3: removing it did not unbalance her bold markers",
      all(_DECODE[st].count("**") % 2 == 0 for st in P.STAGES))
check("item 3: the sentence it sat beside is untouched",
      "**Stay in High-League Positioning.**" in _DECODE[P.STAGE_2_P2])

# Item 4. The positioning check runs on a pass as well as a fail.
check("item 4: the positioning check runs on a pass too",
      "After the verdict, pass or fail," in _INS)
check("item 4: the instructions stay a set of rules",
      len(_INS) < _INS_SANITY_MAX, f"{len(_INS):,} of {_INS_SANITY_MAX:,}")

# Item 6. The block that named the wrong filter is gone from every file.
for _st in P.STAGES:
    check(f"item 6: stage {_st} no longer carries the file-level early-signal block",
          "EARLY SIGNAL FAILING SIGNS" not in _DECODE[_st])
# The guard that matters: a filter must not announce a filter it is not.
_S1_PROFILE = _S1[_S1.index("Marriage Readiness Filter: Dating Profile"):
                  _S1.index("Marriage Readiness Filter: Communication")]
check("item 6: the dating-profile filter never says 'communication'",
      "communication" not in _S1_PROFILE.lower(),
      "a profile decode could name the wrong filter again")

# Her positioning rule from the previous round is untouched by all this.
for _st in P.STAGES:
    check(f"stage {_st}: her positioning rule is still there, word for word",
          _DECODE[_st].count(_CLARITY) == 1)
    check(f"stage {_st}: and still reaches the model before a verdict",
          _CLARITY in P.build(_st, False).prefix)

def _INS_NOW():
    return pathlib.Path("courtship-decoder-instructions.md").read_text(
        encoding="utf-8")

print("\nA written response is given once per man, then connected to")
fresh()
_S1_BLOCKS = P.verdict_block_items(P.STAGE_1)
_FAIL_NAME, _FAIL_BODY = _S1_BLOCKS[0]
_OTHER_NAME, _OTHER_BODY = _S1_BLOCKS[2]

check("a block can be named, not just scored",
      P.match_verdict(_FAIL_BODY, P.STAGE_1)[0] == _FAIL_NAME,
      str(P.match_verdict(_FAIL_BODY, P.STAGE_1)[0]))
check("the old scoring still answers the same way",
      P.verdict_score(_FAIL_BODY, P.STAGE_1) == 1.0)
check("a reply carrying no block names none",
      P.match_verdict("What stage are you in with this man, Queen?", P.STAGE_1)
      == (None, 0.0))

cid = say("Stage 1. His bio is blank and there are photos of him with other "
          "women.").json()["conversation_id"]
NEXT_REPLY["text"] = _FAIL_BODY
first = say("Yes, that is him.", cid).json()
conv = A.STORE.get(cid)
check("the block that landed is recorded against the man",
      conv.blocks_delivered == [_FAIL_NAME], str(conv.blocks_delivered))
check("nothing was said before it, so nothing was withheld",
      CALLS[-1]["system"].already_said is None)

NEXT_REPLY["text"] = "this new behavior changes things... and here is what it adds."
say("He just texted asking me out for Friday.", cid)
sent = CALLS[-1]["system"]
check("the next turn is told what he has already been given",
      sent.already_said is not None and _FAIL_NAME in sent.already_said)
check("it is told plainly that it cannot be said again",
      "cannot be said again" in sent.already_said)
check("and that an unused block is still delivered as written",
      "still delivered exactly as written" in sent.already_said)

print("\nIt is a per-block rule, not a gag on every written response")
NEXT_REPLY["text"] = _OTHER_BODY
say("He has not planned anything for two weeks either.", cid)
conv = A.STORE.get(cid)
check("a different block, not yet used for this man, is recorded too",
      conv.blocks_delivered == [_FAIL_NAME, _OTHER_NAME],
      str(conv.blocks_delivered))
NEXT_REPLY["text"] = "Anything else?"
say("okay", cid)
_said = CALLS[-1]["system"].already_said
check("both are now listed back", _FAIL_NAME in _said and _OTHER_NAME in _said)
check("the same block is never listed twice",
      _said.count(_FAIL_NAME) == 1)

print("\nThe cached part of the prompt is not disturbed by any of it")
_plain = P.build(P.STAGE_1, True)
_with = P.build(P.STAGE_1, True, [_FAIL_NAME])
check("the prefix is byte for byte the same", _plain.prefix == _with.prefix)
check("the lesson block is byte for byte the same", _plain.lesson == _with.lesson)
check("the list rides last, after both", _with.text.endswith(_with.already_said))
check("and it is small enough to never take a cache breakpoint",
      len(_with.already_said) < A.MIN_CACHEABLE_CHARS,
      f"{len(_with.already_said)} vs {A.MIN_CACHEABLE_CHARS}")

print("\nA new man is told nothing about the last one")
NEXT_REPLY["text"] = "What stage are you in with this man, Queen?"
A.LIMITER._hits.clear()
A.LEDGER._usd = 0.0          # the ceiling is not what is under test here
_moved = say("I want to decode another man now.", cid)
check("the move itself was served", _moved.status_code == 200,
      f"HTTP {_moved.status_code}")
conv = A.STORE.get(cid)
check("she was moved on", conv.man_number == 2)
check("what was said to the first man is not carried over",
      conv.blocks_delivered == [], str(conv.blocks_delivered))
check("so his written responses are available again for the new man",
      CALLS[-1]["system"].already_said is None)

print("\nHer connecting line is in the instructions")
check("the rule is there",
      "Give a written response once per man" in _INS_NOW())
check("with her wording for the connection",
      "this new behavior changes things..." in _INS_NOW())
check("and it still says an unused response is delivered as written",
      "using the lessons" in _INS_NOW())

# ---------------------------------------------------------------------------
# Her Sept 4 second document. Three faults she hit in live use, each replayed
# with the wording she actually typed.
# ---------------------------------------------------------------------------
print("\nBug 1: the three-date man who passed and should have failed")

_OFFER = ("He leads all forward movement, and that includes this season. "
          "Monitoring him is you quietly taking the wheel. Take your hands "
          "off it. Want to decode another man?")
_THREE = ("yes - I've been on three dates with a man I met online. All three "
          "dates have been great. He plans the dates, initiates consistently, "
          "and we have really good chemistry. He's asked me a lot about my "
          "life, career, family, and what I enjoy. But he hasn't asked me "
          "anything about my relationship goals or what I'm ultimately "
          "looking for.")
_GOALS_SIGN = "does not naturally ask about her desire for marriage"

check("her 'yes -' answer to the offer now reads as a new man",
      P.mentions_a_new_man(_THREE, _OFFER),
      "the stage stayed sticky on the last man and the wrong file was loaded")
check("and it lands on Stage 2 Phase 2, not Stage 4 and not Stage 1",
      P.resolve_stage(_THREE, None) == P.STAGE_2_P2,
      P.resolve_stage(_THREE, None))
check("which is the one file carrying the sign she says should fail him",
      _GOALS_SIGN in P.read_file(P.DECODE_FILE[P.STAGE_2_P2]).lower())
check("no other decode file carries it, so no other file could fail him",
      [st for st in P.STAGES
       if _GOALS_SIGN in P.read_file(P.DECODE_FILE[st]).lower()]
      == [P.STAGE_2_P2])
check("Stage 1, where it used to land, says the opposite",
      "do not treat the absence of this conversation as a failing sign"
      in P.read_file(P.DECODE_FILE[P.STAGE_1]))

print("\n  and the guards that stop it firing when she has not moved on")
check("a bare yes with no offer behind it is not a new man",
      not P.mentions_a_new_man("yes", "Tell me what's been happening."))
check("a bare yes with no previous reply at all is not a new man",
      not P.mentions_a_new_man("okay", None))
check("pushback after the offer stays with the man in play",
      not P.mentions_a_new_man("but he's been busy with work", _OFFER))
check("naming one still works without any offer",
      P.mentions_a_new_man("another man - he texted me", None))
check("the old single-argument call still behaves as it did",
      P.mentions_a_new_man("someone else now") and not P.mentions_a_new_man("yes"))

print("\n  a date count alone settles the stage, since Stage 1 is before date one")
check("'three dates' with no stage named is Phase 2",
      P.resolve_stage("I've been on three dates with him", None) == P.STAGE_2_P2)
check("'two dates' with no stage named is Phase 1",
      P.resolve_stage("we've had two dates so far", None) == P.STAGE_2_P1)
check("'one date' is Phase 1, not Stage 1",
      P.resolve_stage("just one date so far", None) == P.STAGE_2_P1)
check("a stage she names still beats the date count",
      P.resolve_stage("stage 4, and we had ten dates before he proposed", None)
      == P.STAGE_4)
check("no dates and no stage still opens at Stage 1",
      P.resolve_stage("he messaged me on the app", None) == P.STAGE_1)
check("an established later stage is not dragged back by a date count",
      P.resolve_stage("we had three dates before the proposal", P.STAGE_4)
      == P.STAGE_4)

print("\nBug 2: a message holding both his behavior and something out of scope")
check("the scope gate no longer fires on a behavior it has no read on",
      "behavior you have no read on" not in _INS,
      "this clause let it refuse a message that described what he did")
check("it now refuses only when she gives no behavior of his",
      "and gives you no behavior of his" in _INS)
check("and it is told plainly to decode the half that is his behavior",
      "decode his behavior and leave the rest" in _INS)
check("never refuse a message that tells you what he did",
      "Never refuse a message that tells you what he did" in _INS)
check("the rule forbidding a partial answer is gone, it contradicted this",
      "Do not give a partial answer first" not in _INS)
check("the refusal line itself is unchanged",
      "That's outside what I do here, beautiful." in _INS)

print("\nBug 3: a refresh starts her cleanly instead of resuming invisibly")
_CHAT = pathlib.Path(__file__).with_name("static").joinpath("chat.html") \
    .read_text(encoding="utf-8")
check("the conversation id is no longer kept in sessionStorage",
      "sessionStorage." not in _CHAT,
      "a refresh resumed the old man while the page redrew empty")
check("nor in localStorage, which would survive even closing the tab",
      "localStorage." not in _CHAT)
check("the id starts empty on every page load",
      "var conversationId = null;" in _CHAT)
check("the id still rides on the request while the page is open",
      "conversation_id: conversationId" in _CHAT)
check("and is still cleared when the conversation is refused",
      "conversation_limit" in _CHAT)

print("\n  and the instructions are still a set of rules after all of it")
check("within the sanity bound",
      len(_INS) < _INS_SANITY_MAX, f"{len(_INS):,} of {_INS_SANITY_MAX:,}")

print("\nA stage-specific rule stays in the decode files, not the shared one")
# With the 8,000 character field retired, this rule could now be moved into the
# instructions. It must not be, and the reason is scope, not space. Her rule says
# texting is not a signal "In Stage 1 and Stage 2 Phase 1". Stage 2 Phase 2,
# Filter 1 fails a man for exactly that behaviour. The instructions are loaded on
# every request, so putting the rule there would stand that licence next to the
# failing sign it contradicts — the same shape as the early-signal block that
# made a dating-profile decode announce the communication filter.
_TEXTING = ("In Stage 1 and Stage 2 Phase 1, inconsistent texting or calling "
            "says nothing about his readiness for marriage.")
_carries = [st for st in P.STAGES if _TEXTING in P.read_file(P.DECODE_FILE[st])]
check("her texting rule is in exactly the two stages it names",
      _carries == [P.STAGE_1, P.STAGE_2_P1], str(_carries))
check("and never in the file loaded for every stage",
      _TEXTING not in _INS)
check("because Phase 2 does treat that behaviour as a failing sign",
      "He becomes inconsistent in his communication or effort"
      in P.read_file(P.DECODE_FILE[P.STAGE_2_P2]))

print(f"\n{PASSED} passed, {FAILED} failed")
sys.exit(1 if FAILED else 0)
