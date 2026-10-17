"""Robust JSON extraction from model output."""
from __future__ import annotations

import json
import re
from typing import Any


class ExtractionError(ValueError):
    """No valid JSON object/array could be extracted from model text."""


_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)
_FENCE = re.compile(r"```(?:json|JSON)?\s*\n?(.*?)```", re.S)
_TRAILING_COMMA = re.compile(r",(\s*[}\]])")


def _strip_think(text: str) -> str:
    text = _THINK.sub("", text)
    # unterminated think block (truncated output): drop from the tag on
    m = re.search(r"<think>", text, re.I)
    if m:
        text = text[: m.start()]
    # orphan closing tag (template opened the think block): keep what follows
    if "</think>" in text.lower():
        text = re.split(r"</think>", text, flags=re.I)[-1]
    return text


def _try_load(s: str) -> Any:
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return json.loads(_TRAILING_COMMA.sub(r"\1", s))


def _balanced_end(text: str, start: int) -> int | None:
    depth, in_str, esc = 0, False, False
    for j in range(start, len(text)):
        c = text[j]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c in "{[":
            depth += 1
        elif c in "}]":
            depth -= 1
            if depth == 0:
                return j + 1
    return None


def _scan(text: str, want: type | tuple) -> Any:
    dec = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch not in "{[":
            continue
        try:
            val, _ = dec.raw_decode(text[i:])
        except json.JSONDecodeError:
            end = _balanced_end(text, i)
            if end is None:
                continue
            try:
                val = _try_load(text[i:end])
            except json.JSONDecodeError:
                continue
        if isinstance(val, want):
            return val
    raise ExtractionError("no JSON found")


def extract_json(text: str | None, expect: str = "any") -> Any:
    """Extract a JSON object/array. expect: 'object' | 'array' | 'any'."""
    if not text or not text.strip():
        raise ExtractionError("empty model output")
    want = {"object": dict, "array": list, "any": (dict, list)}[expect]
    cleaned = _strip_think(text)
    candidates = [m.group(1) for m in _FENCE.finditer(cleaned)] + [cleaned]
    for cand in candidates:
        cand = cand.strip()
        try:
            val = _try_load(cand)
            if isinstance(val, want):
                return val
        except json.JSONDecodeError:
            pass
        try:
            return _scan(cand, want)
        except ExtractionError:
            continue
    raise ExtractionError(f"no JSON {expect} found in model output")


def extract_object(text: str | None) -> dict:
    return extract_json(text, "object")


def extract_array(text: str | None) -> list:
    return extract_json(text, "array")
