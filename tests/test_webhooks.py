"""Inbound webhooks: signature verification, trigger rules, and the merge decision.

These are the only unauthenticated endpoints on the platform, and one of them can merge
code. So the tests care most about the refusal paths: an unsigned request must be rejected
before its payload means anything, and a merge must be declined for every reason GitHub
itself would decline it.
"""
import hashlib
import hmac
import json

import pytest

from src.integration import webhooks as wh


# ------------------------------------------------------------------ GitHub signatures
def _sign(secret: str, raw: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def test_a_correctly_signed_github_payload_is_accepted(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "s3cret")
    raw = json.dumps({"action": "submitted"}).encode()
    assert wh.verify_github(raw, _sign("s3cret", raw)) is True


def test_a_tampered_body_fails_verification(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "s3cret")
    raw = b'{"action":"submitted"}'
    sig = _sign("s3cret", raw)
    assert wh.verify_github(b'{"action":"closed"}', sig) is False


@pytest.mark.parametrize("header", ["", "sha256=", "deadbeef", "sha1=abc", "sha256=zz"])
def test_malformed_or_missing_signatures_are_rejected(monkeypatch, header):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "s3cret")
    assert wh.verify_github(b"{}", header) is False


def test_an_unconfigured_secret_rejects_everything(monkeypatch):
    """Failing closed matters: an unset secret must not mean 'accept anything'."""
    monkeypatch.delenv("GITHUB_WEBHOOK_SECRET", raising=False)
    raw = b"{}"
    assert wh.verify_github(raw, _sign("anything", raw)) is False
    monkeypatch.delenv("JIRA_WEBHOOK_SECRET", raising=False)
    assert wh.verify_jira("anything") is False


def test_jira_shared_secret_must_match_exactly(monkeypatch):
    monkeypatch.setenv("JIRA_WEBHOOK_SECRET", "abc123")
    assert wh.verify_jira("abc123") is True
    assert wh.verify_jira("abc124") is False
    assert wh.verify_jira("") is False


# ------------------------------------------------------------------ Jira parsing
def test_a_standard_jira_webhook_is_parsed():
    ev = wh.parse_jira_event({"issue": {"key": "SCRUM-5",
                                        "fields": {"summary": "Login", "status": {"name": "Ready for QA"}}}})
    assert ev == {"key": "SCRUM-5", "status": "Ready for QA", "summary": "Login"}


def test_a_transition_changelog_wins_over_the_stale_status():
    """On a transition event the `fields.status` can still be the OLD value."""
    ev = wh.parse_jira_event({
        "issue": {"key": "SCRUM-9", "fields": {"status": {"name": "In Progress"}, "summary": "s"}},
        "changelog": {"items": [{"field": "status", "toString": "Ready for QA"}]}})
    assert ev["status"] == "Ready for QA"


def test_a_flat_automation_payload_is_parsed():
    ev = wh.parse_jira_event({"key": "ABC-12", "status": "Ready for Test", "summary": "x"})
    assert ev["key"] == "ABC-12" and ev["status"] == "Ready for Test"


# ------------------------------------------------------------------ trigger rules
def test_a_trigger_status_starts_a_run(monkeypatch):
    monkeypatch.delenv("JIRA_TRIGGER_STATUSES", raising=False)
    ok, why = wh.should_start_run({"key": "SCRUM-5", "status": "Ready for QA"})
    assert ok and "SCRUM-5" in why


def test_an_unrelated_status_is_ignored_rather_than_run(monkeypatch):
    monkeypatch.delenv("JIRA_TRIGGER_STATUSES", raising=False)
    ok, why = wh.should_start_run({"key": "SCRUM-5", "status": "In Progress"})
    assert not ok and "not a trigger status" in why


def test_trigger_statuses_are_configurable(monkeypatch):
    monkeypatch.setenv("JIRA_TRIGGER_STATUSES", "done, shipped")
    assert wh.trigger_statuses() == ("done", "shipped")
    assert wh.should_start_run({"key": "A-1", "status": "Shipped"})[0] is True


def test_a_payload_without_an_issue_key_never_starts_a_run():
    assert wh.should_start_run({"status": "Ready for QA"})[0] is False


# ------------------------------------------------------------------ the merge decision
def _status(**over):
    base = {"state": "open", "draft": False, "merged": False, "mergeable": True,
            "mergeable_state": "clean", "approvals": 1, "changes_requested": False,
            "checks": [{"name": "playwright", "conclusion": "success"}],
            "checks_pending": 0, "checks_failing": 0}
    base.update(over)
    return base


def test_an_approved_pr_with_green_checks_may_merge():
    ok, why = wh.merge_allowed(_status())
    assert ok and "1 approval" in why


@pytest.mark.parametrize("over,expected", [
    ({"merged": True}, "already merged"),
    ({"state": "closed"}, "closed"),
    ({"draft": True}, "draft"),
    ({"changes_requested": True}, "requested changes"),
    ({"approvals": 0}, "0 approval"),
    ({"checks_pending": 1}, "still running"),
    ({"checks_failing": 1}, "failing"),
    ({"checks": []}, "no status checks"),
    ({"mergeable": False}, "not mergeable"),
])
def test_every_reason_a_merge_must_be_declined(over, expected):
    ok, why = wh.merge_allowed(_status(**over))
    assert ok is False and expected in why


def test_a_pr_with_no_checks_at_all_is_never_auto_merged():
    """Nothing verified the change, so approval alone must not be enough."""
    ok, why = wh.merge_allowed(_status(checks=[], checks_pending=0, checks_failing=0))
    assert ok is False and "no status checks" in why


def test_a_higher_approval_threshold_is_honoured():
    assert wh.merge_allowed(_status(approvals=1), min_approvals=2)[0] is False
    assert wh.merge_allowed(_status(approvals=2), min_approvals=2)[0] is True


# ------------------------------------------------------------------ GitHub event parsing
def test_a_review_event_is_parsed():
    ev = wh.parse_github_review_event({
        "action": "submitted", "review": {"state": "approved", "user": {"login": "alice"}},
        "repo": {}, "repository": {"full_name": "org/repo"},
        "pull_request": {"number": 7, "draft": False, "merged": False}})
    assert ev["action"] == "submitted" and ev["review_state"] == "approved"
    assert ev["repo"] == "org/repo" and ev["number"] == 7 and ev["reviewer"] == "alice"


def test_a_non_approval_review_is_distinguishable():
    ev = wh.parse_github_review_event({
        "action": "submitted", "review": {"state": "CHANGES_REQUESTED"},
        "repository": {"full_name": "o/r"}, "pull_request": {"number": 1}})
    assert ev["review_state"] == "changes_requested"


if __name__ == "__main__":     # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
