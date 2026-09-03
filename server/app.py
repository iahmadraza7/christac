"""
The Courtship Decoder — chat backend.

One small service. Serves a single chat page for embedding in an iframe on
Kajabi, and answers one endpoint, POST /api/chat.

Run:
    python -m uvicorn server.app:app --host 127.0.0.1 --port 8000

Everything configurable lives in .env next to this folder. Nothing about the
model, the key or the allowed domain is hardcoded.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from . import prompt as P
from . import providers
from . import cost
from .state import ConversationStore, RateLimiter

log = logging.getLogger("decoder")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
STATIC = HERE / "static"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
def unescape(value: str) -> str:
    """
    Turn a written-out "\n" into a real newline.

    A setting is one line, so a value that needs a line break has to write it
    rather than take one. Writing it across several lines instead loses
    everything after the first: the parser reads line by line and a
    continuation line has no "=" in it, so it is skipped in silence. That is
    how her approved repeat-decode reply lost its second sentence.
    """
    return value.replace(r"\n", chr(10))


def load_env(env_file: Path | None = None) -> dict:
    """.env next to the project, with real environment variables winning."""
    values = {}
    env_file = env_file or (ROOT / ".env")
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            values[k.strip()] = unescape(v.strip().strip('"').strip("'"))
    # A real environment variable wins over .env, which is the normal
    # deployment convention. It is also a trap: a stray ANTHROPIC_API_KEY left
    # in a shell silently replaces the key in .env, and the only symptom is the
    # other account's billing errors. So say so, loudly, at startup.
    global OVERRIDDEN
    OVERRIDDEN = sorted(k for k, v in values.items()
                        if k in os.environ and os.environ[k] != v)
    values.update(os.environ)
    return values


OVERRIDDEN: list[str] = []


ENV = load_env()


def _int(name: str, default: int) -> int:
    try:
        return int(str(ENV.get(name, default)).strip())
    except ValueError:
        raise SystemExit(f"{name} must be a whole number, got {ENV.get(name)!r}")


def _float(name: str, default: float) -> float:
    try:
        return float(str(ENV.get(name, default)).strip())
    except ValueError:
        raise SystemExit(f"{name} must be a number, got {ENV.get(name)!r}")


PROVIDER_NAME = (ENV.get("PROVIDER") or "").strip()
MODEL = (ENV.get("MODEL") or "").strip()
if not PROVIDER_NAME:
    raise SystemExit("PROVIDER is not set in .env (anthropic, openai or google)")
if not MODEL:
    raise SystemExit("MODEL is not set in .env. There is no default on purpose.")

ALLOWED_ORIGINS = [o.strip().rstrip("/")
                   for o in (ENV.get("ALLOWED_ORIGINS") or "").split(",")
                   if o.strip()]
RATE_LIMIT_PER_MINUTE = _int("RATE_LIMIT_PER_MINUTE", 12)
# Per man, not per session, like the turn limit above. 1,500,000 is 1.27x the
# worst a man can cost inside the 20-turn limit: Stage 1 carries the largest
# lesson file, 56,843 tokens a turn once a verdict lands, and 19 such turns
# plus the growing history reach 1,179,214. So the turn limit always fires
# first and this stays a backstop against something unexpected - a lesson file
# growing, say - rather than a wall a real conversation hits.
#
# It is not the money guard. A full 20-turn man costs about $1, almost all of
# it cache reads; MONTHLY_SPEND_CEILING_USD is what actually stops the bill.
#
# The old 250,000 was reached after 4.4 post-verdict turns, which is what cut
# her conversation off.
MAX_TOKENS_PER_CONVERSATION = _int("MAX_TOKENS_PER_CONVERSATION", 1_500_000)
# 2048, not 1024. Her longer verdicts reach ~650 output tokens, and the
# positioning line comes last in the response shape, so a reply cut short
# loses exactly that line. Output is billed on what is generated, not on
# the ceiling, so a higher ceiling costs nothing when replies stay short.
MAX_OUTPUT_TOKENS = _int("MAX_OUTPUT_TOKENS", 2048)
MAX_MESSAGE_CHARS = _int("MAX_MESSAGE_CHARS", 4000)
CONVERSATION_TTL_MINUTES = _int("CONVERSATION_TTL_MINUTES", 120)
VERDICT_MATCH_THRESHOLD = _float("VERDICT_MATCH_THRESHOLD", 0.35)
TRUST_PROXY = (ENV.get("TRUST_PROXY") or "false").strip().lower() == "true"

# --- cost and overuse controls ------------------------------------------
# Her method ends after the verdict and the next step, so a real conversation
# is a handful of turns. This sits well above that but still stops a runaway.
MAX_TURNS_PER_CONVERSATION = _int("MAX_TURNS_PER_CONVERSATION", 20)

# Repeat decode: how long after a verdict a restatement is treated as going
# back over the same man, and how much of her wording must already have been
# said for it to count as a restatement rather than new behaviour.
REPEAT_WINDOW_TURNS = _int("REPEAT_DECODE_WINDOW_TURNS", 3)
REPEAT_SIMILARITY = _float("REPEAT_DECODE_SIMILARITY", 0.60)

# Approved by her as drafted.
# Overridable from .env so she can replace it without a code change.
REPEAT_REPLY = (unescape(ENV.get("REPEAT_DECODE_REPLY") or "") or
                "We already read him, Queen. Going back over it won't change what "
                "he showed you.\n\n"
                "Want to decode another man?")

MONTHLY_CEILING_USD = _float("MONTHLY_SPEND_CEILING_USD", 50.0)
SPEND_LEDGER = Path(ENV.get("SPEND_LEDGER_PATH") or (HERE / ".spend.json"))

# "5m" is the API default and measured cheapest here: a 1h cache costs 2x to
# write instead of 1.25x, and at this traffic shape the pricier write is not
# repaid. Switch to "1h" only if measurement shows conversations clustering
# within the hour but not within five minutes.
CACHE_TTL = (ENV.get("CACHE_TTL") or "5m").strip()
MIN_CACHEABLE_CHARS = _int("MIN_CACHEABLE_CHARS", 4000)

PROVIDER = providers.build(PROVIDER_NAME, ENV, MODEL, MAX_OUTPUT_TOKENS)
PROVIDER.cache_ttl = CACHE_TTL
PROVIDER.min_cacheable_chars = MIN_CACHEABLE_CHARS
STORE = ConversationStore(ttl_seconds=CONVERSATION_TTL_MINUTES * 60)
LIMITER = RateLimiter(limit=RATE_LIMIT_PER_MINUTE)
RATES = cost.Rates.from_env(ENV)
LEDGER = cost.MonthlyLedger(SPEND_LEDGER, MONTHLY_CEILING_USD)

app = FastAPI(title="The Courtship Decoder", docs_url=None, redoc_url=None)


@app.on_event("startup")
async def _startup() -> None:
    missing = P.preflight()
    if missing:
        raise SystemExit("Missing knowledge files: " + ", ".join(missing))
    log.info("provider=%s model=%s", PROVIDER.name, MODEL)
    for name in OVERRIDDEN:
        log.warning("%s is set in the environment and is DIFFERENT from the "
                    "value in .env — the environment one is being used. If that "
                    "is not what you meant, unset it.", name)
    log.info("rate limit=%d/min  per-man cap=%d tokens / %d turns  ttl=%dm",
             RATE_LIMIT_PER_MINUTE, MAX_TOKENS_PER_CONVERSATION,
             MAX_TURNS_PER_CONVERSATION, CONVERSATION_TTL_MINUTES)
    log.info("monthly ceiling $%.2f, spent so far $%.4f, ledger %s",
             MONTHLY_CEILING_USD, LEDGER.spent(), SPEND_LEDGER)
    if LEDGER.would_exceed():
        log.warning("the monthly ceiling is already reached — the service will "
                    "refuse every message until the ceiling is raised or the "
                    "month rolls over")
    if ALLOWED_ORIGINS:
        log.info("allowed origins: %s", ", ".join(ALLOWED_ORIGINS))
    else:
        log.warning("ALLOWED_ORIGINS is empty — cross-origin requests will be "
                    "refused and the page cannot be embedded anywhere. Set it "
                    "to the Kajabi domain before going live.")


# ---------------------------------------------------------------------------
# Origin allowlist, CORS and framing
# ---------------------------------------------------------------------------
def origin_allowed(origin: str | None) -> bool:
    # No Origin header means a same-origin or non-browser request; the browser
    # only omits it for same-origin GETs, which the allowlist is not for.
    if not origin:
        return True
    return origin.rstrip("/") in ALLOWED_ORIGINS


def cors_headers(origin: str | None) -> dict:
    if not origin or not origin_allowed(origin):
        return {}
    return {
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        "Access-Control-Max-Age": "600",
        "Vary": "Origin",
    }


@app.options("/api/chat")
async def chat_preflight(request: Request) -> Response:
    origin = request.headers.get("origin")
    if not origin_allowed(origin):
        return JSONResponse({"code": "origin_not_allowed"}, status_code=403)
    return Response(status_code=204, headers=cors_headers(origin))


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    # Who may put this page in an iframe. X-Frame-Options is deliberately not
    # sent: it cannot express an allowlist and DENY would block the embed.
    ancestors = " ".join(ALLOWED_ORIGINS) if ALLOWED_ORIGINS else "'none'"
    response.headers["Content-Security-Policy"] = (
        f"frame-ancestors {ancestors}; default-src 'self'; "
        "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
        "connect-src 'self'; img-src 'self' data:; base-uri 'none'; "
        "form-action 'none'"
    )
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def client_ip(request: Request) -> str:
    if TRUST_PROXY:
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------
class ChatIn(BaseModel):
    message: str = Field(min_length=1)
    conversation_id: str | None = None


@app.post("/api/chat")
async def chat(body: ChatIn, request: Request) -> Response:
    origin = request.headers.get("origin")
    extra = cors_headers(origin)

    if not origin_allowed(origin):
        log.warning("refused origin %s", origin)
        return JSONResponse(
            {"code": "origin_not_allowed",
             "detail": "This page is not allowed to be used from that domain."},
            status_code=403)

    # The month's ceiling comes first: past it, nothing may spend at all.
    if LEDGER.would_exceed():
        log.error("monthly ceiling reached: $%.4f of $%.2f", LEDGER.spent(),
                  MONTHLY_CEILING_USD)
        return JSONResponse(
            {"code": "monthly_ceiling_reached",
             "detail": "The Decoder is resting for now. Please check back soon."},
            status_code=503, headers={**extra, "Retry-After": "3600"})

    ip = client_ip(request)
    ok, retry_after = LIMITER.check(ip)
    if not ok:
        return JSONResponse(
            {"code": "rate_limited",
             "detail": "That's a lot at once, beautiful. Give it a moment."},
            status_code=429,
            headers={**extra, "Retry-After": str(retry_after)})

    message = body.message.strip()
    if not message:
        return JSONResponse({"code": "empty_message", "detail": "Say something."},
                            status_code=400, headers=extra)
    if len(message) > MAX_MESSAGE_CHARS:
        return JSONResponse(
            {"code": "message_too_long",
             "detail": f"Keep it under {MAX_MESSAGE_CHARS:,} characters."},
            status_code=413, headers=extra)

    conversation = STORE.get(body.conversation_id)
    if conversation is None:
        conversation = STORE.new(stage=P.STAGE_1)

    # She is meant to move on: the flow closes by offering to decode another
    # man. So when she does, put the last man down — his verdict, his turn
    # count and his stage — before any per-man limit is measured.
    started_new_man = False
    if conversation.messages and P.mentions_a_new_man(message):
        conversation.start_new_man()
        started_new_man = True
        log.info("conv=%s moving to man #%d, counters reset",
                 conversation.id[:8], conversation.man_number)

    # One man cannot be re-litigated indefinitely. This counts only the turns
    # spent on the man in play, so moving to a new man is never punished.
    if conversation.turns_this_man >= MAX_TURNS_PER_CONVERSATION:
        return JSONResponse(
            {"code": "man_turn_limit",
             "detail": "We've been round this one a lot, Queen. Tell me about "
                       "another man, or start fresh when you're ready.",
             "conversation_id": conversation.id},
            status_code=409, headers=extra)

    # The hard cap. Checked before the call so a conversation that is already
    # over budget cannot buy one more expensive turn.
    if conversation.tokens_this_man >= MAX_TOKENS_PER_CONVERSATION:
        return JSONResponse(
            {"code": "conversation_limit",
             "detail": "This conversation has reached its limit. Start a new "
                       "one and we'll pick it up fresh.",
             "conversation_id": conversation.id},
            status_code=409, headers=extra)

    # Which single decode file is in play, from what she has said about THIS
    # man. Never carried over from the last one.
    said = " ".join(conversation.her_messages)
    stage = P.resolve_stage(f"{said} {message}", conversation.stage)
    conversation.stage = stage

    # Going back over a man already decoded cannot reach a different answer,
    # because the verdict follows his behaviour and she has given no new
    # behaviour. Answer without calling the model — that is the saving.
    since = conversation.turns_since_verdict()
    if (conversation.verdict_delivered and since is not None
            and since <= REPEAT_WINDOW_TURNS
            and not P.mentions_a_new_man(message)):
        overlap = P.repeats_earlier_message(message, conversation.her_messages,
                                            REPEAT_SIMILARITY)
        if overlap >= REPEAT_SIMILARITY:
            conversation.repeats_blocked += 1
            conversation.messages += [{"role": "user", "content": message},
                                      {"role": "assistant", "content": REPEAT_REPLY}]
            STORE.touch(conversation)
            log.info("conv=%s repeat decode blocked (overlap %.2f), no API call",
                     conversation.id[:8], overlap)
            return JSONResponse({
                "reply": REPEAT_REPLY,
                "conversation_id": conversation.id,
                "stage": stage,
                "verdict_delivered": True,
                "verdict_just_delivered": False,
                "lesson_loaded": False,
                "verdict_score": 0.0,
                "repeat_decode_blocked": True,
                "started_new_man": False,
                "man_number": conversation.man_number,
                "tokens_used": conversation.tokens_this_man,
                "tokens_remaining": max(0, MAX_TOKENS_PER_CONVERSATION
                                        - conversation.tokens_this_man),
            }, headers=extra)

    system = P.build(stage, conversation.verdict_delivered)
    turn_messages = [*conversation.messages, {"role": "user", "content": message}]

    # Cache key groups turns that share a prefix. Stage and whether the lesson
    # tail is on are the only things that change it — never anything per user.
    cache_key = f"decoder:{stage}:{'lesson' if system.lesson else 'base'}"

    started = time.time()
    try:
        reply = await PROVIDER.call(system, turn_messages, cache_key)
    except providers.ProviderError as e:
        log.error("provider %s failed: %s", PROVIDER.name, e)
        return JSONResponse(
            {"code": "provider_error",
             "detail": "Something went wrong on my side. Try that again.",
             "conversation_id": conversation.id},
            status_code=502, headers=extra)

    conversation.messages = [*turn_messages,
                             {"role": "assistant", "content": reply.text}]
    conversation.tokens_this_man += reply.total_tokens
    conversation.tokens_total += reply.total_tokens
    turn_usd = RATES.usd_for(reply)
    conversation.usd_spent += turn_usd
    month_usd = LEDGER.add(turn_usd)

    # Has a verdict landed? Scored against the decode file that was in play for
    # this reply. Once true it stays true for the conversation.
    score = P.verdict_score(reply.text, stage)
    newly = False
    if not conversation.verdict_delivered and score >= VERDICT_MATCH_THRESHOLD:
        conversation.verdict_delivered = True
        conversation.verdict_turn = conversation.turns
        newly = True

    STORE.touch(conversation)

    log.info(
        "conv=%s man=%d stage=%s turn=%d/%d verdict=%s score=%.2f lesson=%s "
        "sys=%dKB fresh=%d write=%d read=%d out=%d $%.5f tok=%d/%d "
        "conv$%.5f month$%.4f/%.2f %.1fs",
        conversation.id[:8], conversation.man_number, stage,
        conversation.turns_this_man, MAX_TURNS_PER_CONVERSATION,
        conversation.verdict_delivered, score, bool(system.lesson),
        system.chars // 1024, reply.fresh_input_tokens, reply.cache_write_tokens,
        reply.cached_input_tokens, reply.output_tokens, turn_usd,
        conversation.tokens_this_man, MAX_TOKENS_PER_CONVERSATION,
        conversation.usd_spent, month_usd, MONTHLY_CEILING_USD,
        time.time() - started,
    )

    return JSONResponse({
        "reply": reply.text,
        "conversation_id": conversation.id,
        "stage": stage,
        "verdict_delivered": conversation.verdict_delivered,
        "verdict_just_delivered": newly,
        "lesson_loaded": bool(system.lesson),
        "verdict_score": round(score, 3),
        "repeat_decode_blocked": False,
        "started_new_man": started_new_man,
        "man_number": conversation.man_number,
        "tokens_used": conversation.tokens_this_man,
        "tokens_remaining": max(0, MAX_TOKENS_PER_CONVERSATION
                                - conversation.tokens_this_man),
        "turns_this_man": conversation.turns_this_man,
        "turns_remaining_this_man": max(0, MAX_TURNS_PER_CONVERSATION
                                        - conversation.turns_this_man),
    }, headers=extra)


@app.get("/api/health")
async def health() -> dict:
    return {
        "ok": True,
        "provider": PROVIDER.name,
        "model": MODEL,
        "conversations": STORE.count(),
        "origins_configured": len(ALLOWED_ORIGINS),
        "month_spent_usd": round(LEDGER.spent(), 4),
        "month_ceiling_usd": MONTHLY_CEILING_USD,
        "month_remaining_usd": round(LEDGER.remaining(), 4),
        "serving": not LEDGER.would_exceed(),
    }


@app.get("/")
async def page() -> FileResponse:
    return FileResponse(STATIC / "chat.html", media_type="text/html")
