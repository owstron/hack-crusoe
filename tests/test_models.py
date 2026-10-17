import pytest

from agents.jsonx import ExtractionError, extract_json
from agents.models import (ROLE_MODELS, ROLE_TEMPERATURE, FakeClient, HTTPError,
                           call_model)

MSG = [{"role": "user", "content": "x"}]


def nosleep(s):
    return None


def test_roles():
    assert set(ROLE_MODELS) == set(ROLE_TEMPERATURE) == {
        "planner", "channel", "verifier", "summarizer", "improver", "visual"}
    assert ROLE_TEMPERATURE["visual"] is None
    assert all(v == 0 for k, v in ROLE_TEMPERATURE.items() if k != "visual")


def test_jsonx_variants():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Sure! Here: {"a": [1,2,]} hope it helps') == {"a": [1, 2]}
    assert extract_json('<think>{"x":0} hmm</think>{"a": 2}') == {"a": 2}
    assert extract_json('noise [1, 2, 3] tail', "array") == [1, 2, 3]
    assert extract_json('{"s": "a } b"} x') == {"s": "a } b"}
    for bad in ("", "no json", '<think>{"a":1}', '{"a":'):
        with pytest.raises(ExtractionError):
            extract_json(bad)


def test_success_and_recorder():
    recs = []
    c = FakeClient(['```json\n{"ok": true}\n```'])
    r = call_model("planner", MSG, client=c, recorder=recs.append,
                   ctx={"run_id": "r1", "checkpoint_idx": 2, "read_no": 3})
    assert r.ok and r.parsed == {"ok": True} and not r.fallback_used
    assert c.calls[0]["model"] == ROLE_MODELS["planner"] and c.calls[0]["temperature"] == 0
    rec = recs[0]
    assert rec["run_id"] == "r1" and rec["checkpoint_idx"] == 2 and rec["read_no"] == 3
    for k in ("role", "model", "temperature", "prompt", "raw_response",
              "parsed_json", "error", "fallback_used"):
        assert k in rec


def test_visual_omits_temperature():
    c = FakeClient(['{"a":1}'])
    call_model("visual", MSG, client=c)
    assert "temperature" not in c.calls[0]


def test_retry_transient_then_ok():
    c = FakeClient([HTTPError(429), HTTPError(503), '{"a": 1}'])
    r = call_model("channel", MSG, client=c, sleep=nosleep)
    assert r.ok and r.attempts == 3 and len(c.calls) == 3


def test_fallback_after_retries_and_recorded():
    recs = []
    c = FakeClient(["not json"])
    r = call_model("planner", MSG, client=c, recorder=recs.append, fallback={"d": 1},
                   max_retries=1, sleep=nosleep)
    assert not r.ok and r.fallback_used and r.parsed == {"d": 1} and "extraction" in r.error
    assert len(c.calls) == 2 and recs[0]["fallback_used"] and recs[0]["parsed_json"] is None
    assert recs[0]["model"] == ROLE_MODELS["planner"]


def test_nontransient_not_retried():
    c = FakeClient([HTTPError(400)])
    r = call_model("planner", MSG, client=c, sleep=nosleep)
    assert not r.ok and len(c.calls) == 1


def test_schema_check():
    def chk(p):
        if "need" not in p:
            raise ValueError("missing need")
        return p
    c = FakeClient(['{"a":1}', '{"need": 1}'])
    r = call_model("planner", MSG, client=c, schema_check=chk, sleep=nosleep)
    assert r.ok and r.parsed == {"need": 1} and len(c.calls) == 2
    c = FakeClient(['{"a":1}'])
    r = call_model("planner", MSG, client=c, schema_check=chk, max_retries=0)
    assert not r.ok and "schema_error" in r.error


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
