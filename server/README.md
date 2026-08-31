# The Courtship Decoder — chat backend

One small FastAPI service. Serves a single chat page for embedding in an iframe
on Kajabi, and answers one endpoint.

```
server/
  app.py            routes, origin allowlist, rate limit, token cap
  prompt.py         system prompt assembly, stage resolution, verdict detection
  providers.py      one adapter per provider, each with prompt caching
  state.py          in-memory conversations and rate limiter
  static/chat.html  the page
  test_server.py    50 checks, no API calls
```

## Run it

```
pip install fastapi uvicorn httpx
cp .env.example .env          # then fill in PROVIDER, MODEL and the key
python -m uvicorn server.app:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000/`. The service refuses to start if `PROVIDER` or
`MODEL` is missing — there is no default model in the code on purpose.

Run one worker. Conversation state is in memory, so a second worker would keep
its own copy and a conversation would lose its verdict flag whenever it landed
on the other one. If you ever need more than one worker, move `state.py` to
Redis first.

## Tests

```
python -m server.test_server
```

Covers the things that cost money or control access: the lesson file staying
out of the prompt before a verdict, only one decode file ever being loaded,
stage resolution, the origin allowlist, framing, the rate limit, the token cap,
and the shape of the Anthropic cache payload.

## System prompt assembly

Three parts, in this order:

1. `courtship-decoder-instructions.md` — always
2. the ONE decode file for the stage in play — always
3. the matching `L1`–`L5` lesson file — **only after a verdict**

The order is a cost decision, not a style one. Parts 1 and 2 are byte-identical
on every turn of a conversation, so they form a stable prefix the provider can
cache. The lesson file is appended at the end, so switching it on leaves that
cached prefix intact.

Lesson files are 90KB–137KB. Loading one before a verdict roughly quadruples
the prompt for no benefit — measured on a live conversation, 41KB before the
verdict against 172KB after — and the lessons are unreachable before a verdict
by design.

## How a verdict is detected

The instructions forbid the assistant from labelling its own output, so a reply
never contains the literal string `DELIVER AS WRITTEN`. What it contains, when
a verdict lands, is the *body* of one of those blocks with her man's details
swapped in. So the reply is scored against the block bodies of the decode file
that was in play, using 5-word shingle containment.

Measured against 26 real turns in `results/`:

| | score |
|---|---|
| replies that delivered a written block | 0.90 – 1.00 |
| clarifying questions, out-of-scope lines, pushback answers | 0.00 – 0.06 |

`VERDICT_MATCH_THRESHOLD` defaults to 0.35, in the middle of that gap. The flag
is per conversation and one-way: once a verdict has landed the lesson file stays
on for the rest of that conversation.

**Known gap.** One of the 26 turns was a real decode that the model *paraphrased*
instead of reproducing, and it scored 0.06 — the lesson file would not unlock in
that conversation. That is the retrieval-precision limit already written into
`test-plan.md`, not a bug in the detector: no threshold can catch a reply that
shares almost no wording with the block. Every turn logs its score, so near
misses are visible in the log if it turns out to happen often.

## Stage resolution

The stage decides which single decode file loads. It is read from everything she
has said so far: an explicit "stage 2", then "phase 1/2", then a date count
(1–2 dates is Phase 1, 3 or more is Phase 2). It is sticky, and changes only when
she names a different stage.

Before she has named one, Stage 1 loads. The assistant's own first move is to ask
her the stage, so the file swaps on the next turn once she answers.

## Embedding on Kajabi

Set `ALLOWED_ORIGINS` to her Kajabi domain, scheme included, no trailing slash.
It drives both the `Origin` check on the API and the `frame-ancestors` rule that
decides who may put the page in an iframe. `X-Frame-Options` is deliberately not
sent: it cannot express an allowlist, and `DENY` would block the embed.

```html
<iframe src="https://your-service-host/" style="width:100%;height:640px;border:0"
        title="The Courtship Decoder"></iframe>
```

The conversation id lives in the iframe's `sessionStorage`, never a cookie —
third-party cookies are unreliable in an iframe on another domain.

## Limits

| Setting | Default | What it does |
|---|---|---|
| `RATE_LIMIT_PER_MINUTE` | 12 | Requests per IP per minute |
| `MAX_TOKENS_PER_CONVERSATION` | 250000 | Hard ceiling, input + output, from what the provider reports. Checked before each call, so a conversation already over budget cannot buy another turn |
| `MAX_OUTPUT_TOKENS` | 1024 | Per reply |
| `MAX_MESSAGE_CHARS` | 4000 | Per message |
| `CONVERSATION_TTL_MINUTES` | 120 | Idle conversations are dropped |

Set `TRUST_PROXY=true` only behind a proxy you control, or the rate limit can be
bypassed with a forged `X-Forwarded-For`.

## Prompt caching

| Provider | How |
|---|---|
| Anthropic | Explicit. A `cache_control` breakpoint on each system block |
| OpenAI | Automatic above ~1024 tokens; `prompt_cache_key` groups turns sharing a prefix |
| Google | Implicit for supported models, on the same stable-prefix rule |

Verified live on OpenAI: a repeat turn read 36,608 of 37,091 input tokens from
cache. The Anthropic path is unit-tested for payload shape but has not been run
against the live API — that account is out of credit.

## No accounts

No login, no user database, no cookies. Kajabi controls who reaches the page.
A conversation is an opaque random id and its turns, held in memory until it
goes idle.
