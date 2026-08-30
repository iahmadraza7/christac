#!/usr/bin/env python3
"""
Verify the three API keys work. Reads from a .env file next to this script.

.env format (no quotes, no spaces around =):
    OPENAI_API_KEY=sk-...
    ANTHROPIC_API_KEY=sk-ant-...
    GOOGLE_API_KEY=AIza...

Run:  python verify_keys.py

Never prints a key. Only says whether each one works.
"""

import json
import urllib.error
import urllib.request
from pathlib import Path

ENV = Path(__file__).resolve().parent / ".env"


def load_env():
    if not ENV.is_file():
        raise SystemExit(f"No .env file found at {ENV}")
    out = {}
    for line in ENV.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def check(name, url, headers):
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = json.loads(r.read().decode("utf-8"))
        models = body.get("data") or body.get("models") or []
        print(f"  {name:12} OK   ({len(models)} models visible)")
        return True
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        hint = ""
        if e.code in (401, 403):
            hint = "  key rejected, check it was copied whole"
        elif e.code == 429:
            hint = "  rate limited or no billing set up on that account"
        print(f"  {name:12} FAIL HTTP {e.code}{hint}")
        print(f"               {detail[:160]}")
        return False
    except Exception as e:
        print(f"  {name:12} FAIL {e}")
        return False


def main():
    env = load_env()
    missing = [k for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY")
               if not env.get(k)]
    if missing:
        print("Missing from .env: " + ", ".join(missing))

    print("Checking keys:\n")
    results = []

    if env.get("OPENAI_API_KEY"):
        results.append(check(
            "OpenAI",
            "https://api.openai.com/v1/models",
            {"Authorization": f"Bearer {env['OPENAI_API_KEY']}"},
        ))

    if env.get("ANTHROPIC_API_KEY"):
        results.append(check(
            "Anthropic",
            "https://api.anthropic.com/v1/models",
            {"x-api-key": env["ANTHROPIC_API_KEY"],
             "anthropic-version": "2023-06-01"},
        ))

    if env.get("GOOGLE_API_KEY"):
        results.append(check(
            "Google",
            f"https://generativelanguage.googleapis.com/v1beta/models?key={env['GOOGLE_API_KEY']}",
            {},
        ))

    print()
    if results and all(results):
        print("All good. Tell her all three are working.")
    else:
        print("Fix the failures above before running the comparison.")


if __name__ == "__main__":
    raise SystemExit(main())