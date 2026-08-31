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
def load_env() -> dict:
    """.env next to the project, with real environment variables winning."""
    values = {}
    env_file = ROOT / ".env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            values[k.strip()] = v.strip().strip('"').strip("'")
    values.update(os.environ)  # a real environment variable wins over .env
    return values


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
MAX_TOKENS_PER_CONVERSATION = _int("MAX_TOKENS_PER_CONVERSATION", 250_000)
MAX_OUTPUT_TOKENS = _int("MAX_OUTPUT_TOKENS", 1024)
MAX_MESSAGE_CHARS = _int("MAX_MESSAGE_CHARS", 4000)
CONVERSATION_TTL_MINUTES = _int("CONVERSATION_TTL_MINUTES", 120)
VERDICT_MATCH_THRESHOLD = _float("VERDICT_MATCH_THRESHOLD", 0.35)
TRUST_PROXY = (ENV.get("TRUST_PROXY") or "false").strip().lower() == "true"

PROVIDER = providers.build(PROVIDER_NAME, ENV, MODEL, MAX_OUTPUT_TOKENS)
STORE = ConversationStore(ttl_seconds=CONVERSATION_TTL_MINUTES * 60)
LIMITER = RateLimiter(limit=RATE_LIMIT_PER_MINUTE)

app = FastAPI(title="The Courtship Decoder", docs_url=None, redoc_url=None)


@app.on_event("startup")
async def _startup() -> None:
    missing = P.preflight()
    if missing:
        raise SystemExit("Missing knowledge files: " + ", ".join(missing))
    log.info("provider=%s model=%s", PROVIDER.name, MODEL)
    log.info("rate limit=%d/min  conversation cap=%d tokens  ttl=%dm",
             RATE_LIMIT_PER_MINUTE, MAX_TOKENS_PER_CONVERSATION,
             CONVERSATION_TTL_MINUTES)
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

    # The hard cap. Checked before the call so a conversation that is already
    # over budget cannot buy one more expensive turn.
    if conversation.tokens_used >= MAX_TOKENS_PER_CONVERSATION:
        return JSONResponse(
            {"code": "conversation_limit",
             "detail": "This conversation has reached its limit. Start a new "
                       "one and we'll pick it up fresh.",
             "conversation_id": conversation.id},
            status_code=409, headers=extra)

    # Which single decode file is in play, from everything she has said.
    said = " ".join(m["content"] for m in conversation.messages
                    if m["role"] == "user")
    stage = P.resolve_stage(f"{said} {message}", conversation.stage)
    conversation.stage = stage

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
    conversation.tokens_used += reply.total_tokens

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
        "conv=%s stage=%s turn=%d verdict=%s score=%.2f lesson=%s "
        "sys=%dKB in=%d cached=%d out=%d used=%d/%d %.1fs",
        conversation.id[:8], stage, conversation.turns,
        conversation.verdict_delivered, score, bool(system.lesson),
        system.chars // 1024, reply.input_tokens, reply.cached_input_tokens,
        reply.output_tokens, conversation.tokens_used,
        MAX_TOKENS_PER_CONVERSATION, time.time() - started,
    )

    return JSONResponse({
        "reply": reply.text,
        "conversation_id": conversation.id,
        "stage": stage,
        "verdict_delivered": conversation.verdict_delivered,
        "verdict_just_delivered": newly,
        "lesson_loaded": bool(system.lesson),
        "verdict_score": round(score, 3),
        "tokens_used": conversation.tokens_used,
        "tokens_remaining": max(0, MAX_TOKENS_PER_CONVERSATION
                                - conversation.tokens_used),
    }, headers=extra)


@app.get("/api/health")
async def health() -> dict:
    return {
        "ok": True,
        "provider": PROVIDER.name,
        "model": MODEL,
        "conversations": STORE.count(),
        "origins_configured": len(ALLOWED_ORIGINS),
    }


@app.get("/")
async def page() -> FileResponse:
    return FileResponse(STATIC / "chat.html", media_type="text/html")
