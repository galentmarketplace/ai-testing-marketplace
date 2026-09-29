"""Inbound webhooks — the difference between a tool someone runs and a system that runs itself.

Two events close the loop:

  Jira   : a ticket reaches the configured status -> start a run, no human involved.
  GitHub : a pull request is approved -> merge it, but ONLY if GitHub itself agrees that
           every required check passed and the approval stands.

Both are unauthenticated endpoints on the public internet, so both verify a signature before
anything is parsed as meaningful. GitHub signs with HMAC-SHA256 over the raw body; Jira
Automation cannot sign, so it presents a shared secret in a header compared in constant time.
An unsigned or wrongly-signed request is rejected without touching the payload.

The merge decision is deliberately re-derived from GitHub rather than trusted from the event:
a webhook tells us something *happened*, not that merging is *allowed*.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re

# Only these Jira transitions start a run. Anything else is acknowledged and ignored, so a
# noisy project board cannot spam the pipeline.
DEFAULT_TRIGGER_STATUSES = ("ready for qa", "ready for test", "in testing", "qa")


def github_secret() -> str:
    return os.environ.get("GITHUB_WEBHOOK_SECRET", "")


def jira_secret() -> str:
    return os.environ.get("JIRA_WEBHOOK_SECRET", "")


def verify_github(raw: bytes, signature_header: str) -> bool:
    """Constant-time HMAC-SHA256 check of GitHub's X-Hub-Signature-256."""
    secret = github_secret()
    if not secret or not signature_header:
        return False
    algo, _, sent = signature_header.partition("=")
    if algo != "sha256" or not sent:
        return False
    expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sent)


def verify_jira(presented: str) -> bool:
    """Shared-secret check for Jira Automation, which cannot HMAC-sign its requests."""
    secret = jira_secret()
    if not secret or not presented:
        return False
    return hmac.compare_digest(secret, presented)


def trigger_statuses() -> tuple[str, ...]:
    raw = os.environ.get("JIRA_TRIGGER_STATUSES", "")
    vals = tuple(s.strip().lower() for s in raw.split(",") if s.strip())
    return vals or DEFAULT_TRIGGER_STATUSES


_KEY = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")


def parse_jira_event(payload: dict) -> dict:
    """Pull the issue key and the new status out of a Jira webhook or Automation payload.

    Jira sends several shapes (webhook `issue`, Automation `{{issue}}`, a flat custom body),
    so this reads defensively rather than assuming one.
    """
    issue = payload.get("issue") or {}
    fields = issue.get("fields") or {}
    key = issue.get("key") or payload.get("key") or payload.get("issueKey") or ""
    if not key:
        m = _KEY.search(str(payload.get("summary") or payload.get("text") or ""))
        key = m.group(1) if m else ""

    status = ""
    st = fields.get("status")
    if isinstance(st, dict):
        status = st.get("name", "") or ""
    elif isinstance(st, str):
        status = st
    status = status or str(payload.get("status") or "")

    # A transition event carries the destination status in changelog items.
    for item in ((payload.get("changelog") or {}).get("items") or []):
        if (item.get("field") or "").lower() == "status":
            status = item.get("toString") or status

    return {"key": key.strip().upper(), "status": status.strip(),
            "summary": fields.get("summary") or payload.get("summary") or ""}


def should_start_run(event: dict) -> tuple[bool, str]:
    """Decide whether a Jira event warrants a run, and say why not when it does not."""
    if not event.get("key"):
        return False, "no issue key in the payload"
    status = (event.get("status") or "").lower()
    if not status:
        return False, "no status in the payload"
    allowed = trigger_statuses()
    if status not in allowed:
        return False, f"status {event['status']!r} is not a trigger status ({', '.join(allowed)})"
    return True, f"{event['key']} entered {event['status']!r}"


def parse_github_review_event(payload: dict) -> dict:
    pr = payload.get("pull_request") or {}
    return {
        "action": payload.get("action") or "",
        "review_state": ((payload.get("review") or {}).get("state") or "").lower(),
        "repo": ((payload.get("repository") or {}).get("full_name") or ""),
        "number": pr.get("number"),
        "draft": bool(pr.get("draft")),
        "merged": bool(pr.get("merged")),
        "reviewer": (((payload.get("review") or {}).get("user") or {}).get("login") or ""),
    }


def merge_allowed(status: dict, min_approvals: int = 1) -> tuple[bool, str]:
    """Re-derive the merge decision from GitHub's own view of the PR.

    Every negative branch is explicit so the audit log records WHY a merge was declined,
    rather than a bare refusal.
    """
    if status.get("merged"):
        return False, "already merged"
    if status.get("state") != "open":
        return False, f"pull request is {status.get('state')}"
    if status.get("draft"):
        return False, "pull request is a draft"
    if status.get("changes_requested"):
        return False, "a reviewer has requested changes"
    if status.get("approvals", 0) < min_approvals:
        return False, (f"{status.get('approvals', 0)} approval(s), "
                       f"{min_approvals} required")
    if status.get("checks_pending", 0):
        return False, f"{status['checks_pending']} check(s) still running"
    if status.get("checks_failing", 0):
        return False, f"{status['checks_failing']} check(s) failing"
    if not status.get("checks"):
        # No checks at all means nothing verified this change. Never auto-merge that.
        return False, "no status checks ran on this pull request"
    if status.get("mergeable") is False:
        return False, f"not mergeable ({status.get('mergeable_state')})"
    return True, (f"{status.get('approvals')} approval(s), "
                  f"{len(status.get('checks', []))} check(s) green")
