# DELIVERY — The Courtship Decoder

Operator notes. Not for the client.

---

## What ships

| Piece | What it is |
|---|---|
| `server/` | The FastAPI service. Serves the chat page at `/` and answers `POST /api/chat` |
| `courtship-decoder-instructions.md` | The system prompt. Loaded on every turn |
| `01`–`05-*.md` | The five decode files. Exactly one is loaded per turn, chosen by stage |
| `server/lessons/` | The lesson layer, each lesson stored once, composed per stage on demand |
| `L1`–`L5-*-lessons.md` | The same lessons pre-assembled per stage. **For her custom GPT, not used by the server.** Kept because her GPT can only load one file |
| `STRIP-REPORT.md` | What was removed from her source and why. Her review document |
| `kajabi-embed.html` | The iframe block she pastes into Kajabi |

The server reads `server/lessons/`, never the `L1`–`L5` files. Both are built
from the same source and are byte-identical in what they contain.

---

## Deploy target

**Railway**, from this git repository. `railway.json` holds the build and start
configuration; nothing needs setting in the dashboard except environment
variables.

Start command (already in `railway.json`):

```
uvicorn server.app:app --host 0.0.0.0 --port $PORT --workers 1
```

**`--workers 1` is load-bearing, do not raise it.** Conversation state, the rate
limiter and the spend ledger all live in the process. A second worker would keep
its own copy: conversations would lose their verdict flag whenever a request
landed on the other worker, and the monthly ceiling would be as many times too
high as there are workers. Scaling out means moving `server/state.py` and the
ledger to Redis first.

Health check is `/api/health`. It returns the provider, the model, how many
conversations are live, spend against the ceiling, and `serving: false` once the
ceiling is reached.

---

## Environment variables

`.env` does not exist on the host and is not committed. Every setting below is
read from the environment. Verified: the service boots with no `.env` file
present.

### Required — it will not start without these

| Variable | Value | Why |
|---|---|---|
| `PROVIDER` | `anthropic` | Which adapter to use |
| `MODEL` | `claude-opus-5` | No default in code, on purpose |
| `ANTHROPIC_API_KEY` | her key | Only the key for the chosen provider is needed |
| `ALLOWED_ORIGINS` | `https://christa-collins.mykajabi.com` | Drives both the Origin check and who may iframe the page. **Empty means the page can be embedded nowhere** |

`PORT` is set by Railway itself. The service does not read it; the start command
passes it to uvicorn.

### Worth setting

| Variable | Default | Notes |
|---|---|---|
| `MONTHLY_SPEND_CEILING_USD` | `50.00` | Past this the service returns 503 and calls nothing |
| `TRUST_PROXY` | `false` | **Set to `true` on Railway.** Behind its proxy the socket address is the proxy, so per-IP rate limiting needs `X-Forwarded-For` |
| `SPEND_LEDGER_PATH` | `server/.spend.json` | See the warning below |

### Tuning, safe to leave alone

`RATE_LIMIT_PER_MINUTE` 12 · `MAX_TURNS_PER_CONVERSATION` 20 (per man, not per
session) · `MAX_TOKENS_PER_CONVERSATION` 250000 · `MAX_OUTPUT_TOKENS` 1024 ·
`MAX_MESSAGE_CHARS` 4000 · `CONVERSATION_TTL_MINUTES` 120 ·
`VERDICT_MATCH_THRESHOLD` 0.35 · `REPEAT_DECODE_WINDOW_TURNS` 3 ·
`REPEAT_DECODE_SIMILARITY` 0.60 · `REPEAT_DECODE_REPLY` (her approved line) ·
`CACHE_TTL` 5m · `MIN_CACHEABLE_CHARS` 4000

Prices, per million tokens, defaulted to the published `claude-opus-5` rates:
`PRICE_INPUT_PER_MTOK` 5.00 · `PRICE_OUTPUT_PER_MTOK` 25.00 ·
`PRICE_CACHE_WRITE_PER_MTOK` 6.25 · `PRICE_CACHE_READ_PER_MTOK` 0.50.
Change these if the model or the price list changes; nothing else needs to know.

### The trap that cost an hour

`load_env()` lets a real environment variable beat `.env`. A stray
`ANTHROPIC_API_KEY` in a shell silently replaced the key in `.env` and the only
symptom was another account's billing errors. The service now logs a warning at
startup naming any variable the environment overrides. **If something is
mysteriously failing on billing, read the startup log first.**

---

## The spend ledger

`MonthlyLedger` writes the running month's spend to `SPEND_LEDGER_PATH`.

**Railway's filesystem is ephemeral.** Every redeploy and every restart wipes the
ledger, so the monthly ceiling silently resets to zero. That makes it a guard
against a runaway conversation, not a reliable monthly cap.

If the cap has to hold across restarts, either attach a Railway volume and point
`SPEND_LEDGER_PATH` at it, or set a hard spend limit in the Anthropic console —
which is the stronger control anyway, because it holds even if this service has
a bug.

---

## Redeploying

Railway watches the repository.

```
git push
```

That is the whole flow. Railway builds with Nixpacks, installs
`requirements.txt`, runs the health check, and only then routes traffic to the
new deployment.

To deploy without pushing (a local build):

```
railway up
```

Verify afterwards:

```
curl https://YOUR-SERVICE.up.railway.app/api/health
```

Expect `"ok": true`, the right model, and `"serving": true`.

---

## Rolling back

**Fastest, no git:** Railway dashboard → the service → Deployments → pick the
last good one → Redeploy. It rebuilds that commit; environment variables are not
part of the rollback, so a bad variable must be fixed by hand.

**In git**, when the bad change should leave the history too:

```
git revert <bad commit>
git push
```

Prefer revert over force-pushing. The client's knowledge files live in this
repository and a force-push can lose work that only exists here.

**If a rollback is because the bill ran away:** set
`MONTHLY_SPEND_CEILING_USD=0.01` in Railway first. That stops the service
answering within seconds, which is faster than any redeploy.

---

## Running it locally

```
pip install -r requirements.txt
cp .env.example .env          # then fill in PROVIDER, MODEL and the key
python -m uvicorn server.app:app --port 8000
```

Tests, which make no API calls and cost nothing:

```
python -m server.test_server
```

---

## Known limits, all measured

- **Cost.** A three-message conversation measured **$0.053** once the prompt
  cache is warm, and **$0.380** on the first conversation after a quiet spell,
  when the lesson layer has to be written into the cache. The cold figure is
  dominated by 42,844 lesson tokens and cannot come down without sending fewer
  lessons, which would change behaviour.
- **Verdict detection** compares the reply against the `DELIVER AS WRITTEN`
  blocks of the decode file in play. Measured on 26 real turns: delivered blocks
  score 0.90–1.00, everything else 0.06 or below. One real decode in that sample
  was paraphrased rather than reproduced and scored 0.058, so its conversation
  would not have unlocked the lessons. Every turn logs its score.
- **Single process only.** See the workers note above.
