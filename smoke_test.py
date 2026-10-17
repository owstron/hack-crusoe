"""Check that the Crusoe and You.com API keys work.

Usage: .venv/bin/python smoke_test.py [--list-models]
"""
import os
import sys
import time

import httpx
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

CRUSOE_BASE_URL = "https://api.inference.crusoecloud.com/v1"
CRUSOE_MODEL = os.environ.get("CRUSOE_MODEL", "zai-org/GLM-5.3-Flash")


def check_crusoe(list_models):
    key = os.environ.get("CRUSOE_API_KEY")
    if not key:
        return "CRUSOE_API_KEY not set"
    client = OpenAI(api_key=key, base_url=CRUSOE_BASE_URL)
    if list_models:
        try:
            for m in client.models.list().data:
                print("   ", m.id)
        except Exception as e:
            print(f"    (model listing unavailable: {e})")
    start = time.time()
    resp = client.chat.completions.create(
        model=CRUSOE_MODEL,
        messages=[{"role": "user", "content": "Reply with exactly: pong"}],
        # Reasoning models spend tokens thinking before answering.
        max_tokens=1000,
    )
    text = (resp.choices[0].message.content or "").strip()
    if not text:
        return f"empty response (finish_reason={resp.choices[0].finish_reason})"
    return f"ok ({CRUSOE_MODEL}, {time.time() - start:.1f}s): {text!r}"


def check_you():
    key = os.environ.get("YDC_API_KEY")
    if not key:
        return "YDC_API_KEY not set"
    start = time.time()
    resp = httpx.post(
        "https://ydc-index.io/v1/search",
        headers={"X-API-Key": key},
        json={"query": "Crusoe Cloud managed inference", "count": 3},
        timeout=30,
    )
    resp.raise_for_status()
    return f"ok ({time.time() - start:.1f}s): {str(resp.json())[:200]}..."


def main():
    failed = False
    for name, check in [
        ("Crusoe", lambda: check_crusoe("--list-models" in sys.argv)),
        ("You.com", check_you),
    ]:
        try:
            result = check()
        except Exception as e:
            result = f"FAILED: {e}"
        if not result.startswith("ok"):
            failed = True
        print(f"{name}: {result}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
