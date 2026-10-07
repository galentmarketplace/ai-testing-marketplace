"""Parsing the model's reply must not throw away a good answer.

Live defect: the Go test generator lost its work on roughly one run in three with
`malformed JSON from the model: Extra data: line 3 column 1 (char 2193)`. The reply WAS
valid — the model had written its object and then written something after it. Taking
everything between the first `{` and the last `}` spans both, so nothing parsed, the
coverage fix silently did not happen, and the report showed the gap as if no attempt had
been made.
"""
import json

import pytest

from src import llm

ANSWER = {"files": [{"path": "internal/cart/cart_generated_test.go", "content": "package cart\n"}],
          "skipped": []}


def _reply(text, monkeypatch):
    monkeypatch.setattr(llm, "call_llm", lambda *a, **k: text)
    return llm.call_llm_json("go_test_agent", "sys", "user")


def test_a_bare_object_still_parses(monkeypatch):
    assert _reply(json.dumps(ANSWER), monkeypatch) == ANSWER


def test_trailing_prose_after_the_object_is_ignored(monkeypatch):
    assert _reply(json.dumps(ANSWER) + "\n\nThese tests cover the happy path.", monkeypatch) == ANSWER


def test_a_preamble_before_the_object_is_ignored(monkeypatch):
    assert _reply("Here are the tests:\n" + json.dumps(ANSWER), monkeypatch) == ANSWER


def test_a_second_object_after_the_answer_does_not_break_the_reply(monkeypatch):
    """The exact live failure: Extra data at line 3."""
    text = json.dumps(ANSWER) + "\n\n" + json.dumps({"note": "done"})
    assert _reply(text, monkeypatch) == ANSWER


def test_a_fenced_block_is_preferred_over_surrounding_prose(monkeypatch):
    text = "Some notes {not json}\n```json\n" + json.dumps(ANSWER) + "\n```\nThanks!"
    assert _reply(text, monkeypatch) == ANSWER


def test_two_fenced_blocks_yield_the_richer_one(monkeypatch):
    text = ("```json\n" + json.dumps({"ok": True}) + "\n```\n"
            "```json\n" + json.dumps(ANSWER) + "\n```")
    assert _reply(text, monkeypatch) == ANSWER


def test_braces_inside_strings_do_not_confuse_the_scan(monkeypatch):
    answer = {"files": [{"content": "func f() { return map[string]int{\"a\": 1} }"}]}
    assert _reply(json.dumps(answer), monkeypatch) == answer


def test_a_truncated_reply_still_reports_itself_as_truncated(monkeypatch):
    """This must NOT be silently tolerated — it means the output budget is too small."""
    with pytest.raises(ValueError, match="cut off"):
        _reply('{"files": [{"path": "a_test.go", "content": "package a', monkeypatch)


def test_a_reply_with_no_json_at_all_is_an_error(monkeypatch):
    with pytest.raises(ValueError, match="No JSON object"):
        _reply("I cannot help with that.", monkeypatch)


def test_a_json_array_reply_is_not_mistaken_for_an_object(monkeypatch):
    with pytest.raises(ValueError):
        _reply('[{"path": "a_test.go"}]', monkeypatch)


if __name__ == "__main__":     # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
