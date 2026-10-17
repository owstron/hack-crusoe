"""You.com research tool for the improver, with a disk cache.

    from improve.research import research, search, RESEARCH_TOOL
    r = research("why does retargeting show low incremental lift despite strong last-click CPA?")
    r["answer"], r["sources"]   # sources: [{"url", "title"}]

- Cached by (endpoint, effort, normalized question) in research/cache/, committed to git, so reruns
  and other machines get identical answers. Errors are returned, never raised, and never cached.
- Results only shape which edits the improver proposes; the gate decides on the simulator.
- Set YDC_DISABLED=1 for the "improver without You.com" comparison run.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time

import httpx

CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "research", "cache")
RESEARCH_URL = "https://api.you.com/v1/research"
SEARCH_URL = "https://ydc-index.io/v1/search"
MAX_ANSWER_CHARS = 4000
MAX_SOURCES = 6

# OpenAI-format tool definition, for exposing research() to the improver model.
RESEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "research",
        "description": "Research a question on the web (You.com) and get a cited answer. Use it to understand "
                       "why the playbook failed, e.g. how advertisers measure true incremental lift. One call per round.",
        "parameters": {
            "type": "object",
            "properties": {"question": {"type": "string", "description": "A specific research question"}},
            "required": ["question"],
        },
    },
}


def _key(*parts: str) -> str:
    norm = [re.sub(r"\s+", " ", p.strip().lower()) for p in parts]
    return hashlib.sha1("|".join(norm).encode()).hexdigest()[:16]


def _cached(kind: str, key: str, fetch) -> dict:
    if os.environ.get("YDC_DISABLED") == "1":  # checked first: the comparison run must not see cached answers
        return {"answer": "(web research disabled for this run)", "sources": [], "cached": False, "disabled": True}
    path = os.path.join(CACHE_DIR, f"{kind}-{key}.json")
    if os.path.exists(path):
        with open(path) as f:
            return {**json.load(f), "cached": True}
    api_key = os.environ.get("YDC_API_KEY")
    if not api_key:
        return {"answer": "", "sources": [], "cached": False, "error": "YDC_API_KEY not set"}
    t0 = time.time()
    try:
        result = fetch(api_key)
    except (httpx.HTTPError, ValueError, KeyError) as e:
        return {"answer": "", "sources": [], "cached": False, "error": f"{type(e).__name__}: {e}"}
    result["seconds"] = round(time.time() - t0, 1)
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(path, "w") as f:
        json.dump(result, f, indent=2)
    return {**result, "cached": False}


def research(question: str, effort: str = "lite", timeout: float = 90) -> dict:
    """Cited answer from the You.com Research API. Returns {question, answer, sources, cached, [error]}."""
    def fetch(api_key):
        r = httpx.post(RESEARCH_URL, headers={"X-API-Key": api_key},
                       json={"input": question, "research_effort": effort}, timeout=timeout)
        r.raise_for_status()
        out = r.json()["output"]
        return {
            "question": question, "effort": effort,
            "answer": (out.get("content") or "")[:MAX_ANSWER_CHARS],
            "sources": [{"url": s.get("url"), "title": s.get("title")} for s in (out.get("sources") or [])][:MAX_SOURCES],
        }
    return _cached("research", _key("research", effort, question), fetch)


def search(query: str, count: int = 5, timeout: float = 30) -> dict:
    """Web results from You.com Search (~1s). Returns {question, answer, sources, cached, [error]};
    `answer` is the concatenated snippets so both tools can be used the same way."""
    def fetch(api_key):
        r = httpx.post(SEARCH_URL, headers={"X-API-Key": api_key}, json={"query": query, "count": count},
                       timeout=timeout)
        r.raise_for_status()
        web = (r.json().get("results") or {}).get("web") or []
        lines = [f"- {w.get('title')}: {' '.join(w.get('snippets') or [w.get('description') or ''])}" for w in web]
        return {
            "question": query,
            "answer": "\n".join(lines)[:MAX_ANSWER_CHARS],
            "sources": [{"url": w.get("url"), "title": w.get("title")} for w in web][:MAX_SOURCES],
        }
    return _cached("search", _key("search", str(count), query), fetch)


def format_for_prompt(result: dict) -> str:
    """Compact text block for the improver prompt."""
    if result.get("error"):
        return f"(research failed: {result['error']})"
    src = "\n".join(f"[{i + 1}] {s['title']} - {s['url']}" for i, s in enumerate(result.get("sources") or []))
    return f"{result.get('answer', '')}\n\nSources:\n{src}" if src else result.get("answer", "")


if __name__ == "__main__":
    import sys

    from dotenv import load_dotenv

    load_dotenv(os.path.join(os.path.dirname(CACHE_DIR), "..", ".env"))
    q = " ".join(sys.argv[1:]) or "why does retargeting show low incremental lift despite strong last-click CPA?"
    for fn in (search, research, research):  # second research call should hit the cache
        r = fn(q)
        print(f"{fn.__name__}: cached={r['cached']} seconds={r.get('seconds')} error={r.get('error')} "
              f"sources={len(r['sources'])} answer={r['answer'][:150]!r}")
