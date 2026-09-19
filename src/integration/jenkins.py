"""Jenkins integration — trigger a job, wait for it, and pull back the test report.

Auth: JENKINS_URL + JENKINS_USER + JENKINS_TOKEN (an API token from the user's
Jenkins profile). All calls go through the REST API via stdlib urllib. Supports
folder jobs ("folder/job") and CSRF crumbs. Every function raises on HTTP error;
the agent decides how to surface it.
"""
import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request


def _base() -> str:
    return os.environ.get("JENKINS_URL", "").rstrip("/")


def configured() -> bool:
    return bool(_base() and os.environ.get("JENKINS_USER") and os.environ.get("JENKINS_TOKEN"))


def _auth_header() -> str:
    raw = f"{os.environ.get('JENKINS_USER','')}:{os.environ.get('JENKINS_TOKEN','')}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def _job_path(job: str) -> str:
    """'folder/pipeline' -> '/job/folder/job/pipeline' (Jenkins folder addressing)."""
    parts = [p for p in job.strip("/").split("/") if p]
    return "".join(f"/job/{urllib.parse.quote(p)}" for p in parts)


def _req(method: str, url: str, body: bytes | None = None, headers: dict | None = None):
    req = urllib.request.Request(url, method=method, data=body,
                                 headers={"Authorization": _auth_header(),
                                          "User-Agent": "agentic-testing-pipeline", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode(errors="ignore")[:300]
        except Exception:
            pass
        raise urllib.error.HTTPError(e.url, e.code, f"{method} {url} → {e.reason}: {detail}", e.headers, None)


def _crumb() -> dict:
    """CSRF crumb header if the Jenkins instance requires it (best-effort)."""
    try:
        _, _, raw = _req("GET", _base() + "/crumbIssuer/api/json")
        d = json.loads(raw)
        return {d["crumbRequestField"]: d["crumb"]}
    except Exception:
        return {}


def _json(url: str) -> dict:
    _, _, raw = _req("GET", url)
    return json.loads(raw)


def trigger(job: str, params: dict | None = None) -> str:
    """Kick off a build. Returns the queue-item URL (poll it for the build number)."""
    jp = _base() + _job_path(job)
    crumb = _crumb()
    if params:
        url = jp + "/buildWithParameters?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    else:
        url = jp + "/build"
    status, headers, _ = _req("POST", url, headers=crumb)
    return headers.get("Location", "").rstrip("/")


def await_build_number(queue_url: str, timeout: int = 120, interval: int = 3) -> int:
    """Poll the queue item until Jenkins assigns it an executable (build) number."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        d = _json(queue_url + "/api/json")
        ex = d.get("executable")
        if ex and ex.get("number"):
            return int(ex["number"])
        if d.get("cancelled"):
            raise RuntimeError("Jenkins queue item was cancelled")
        time.sleep(interval)
    raise TimeoutError("timed out waiting for Jenkins to start the build")


def await_result(job: str, number: int, timeout: int = 1800, interval: int = 5) -> str:
    """Poll a running build until it finishes. Returns SUCCESS | FAILURE | UNSTABLE | ABORTED."""
    jp = _base() + _job_path(job)
    deadline = time.time() + timeout
    while time.time() < deadline:
        d = _json(f"{jp}/{number}/api/json")
        if not d.get("building") and d.get("result"):
            return d["result"]
        time.sleep(interval)
    raise TimeoutError("timed out waiting for the Jenkins build to finish")


def test_report(job: str, number: int) -> dict | None:
    """The JUnit test report for a build, if the job publishes one."""
    jp = _base() + _job_path(job)
    try:
        d = _json(f"{jp}/{number}/testReport/api/json")
    except urllib.error.HTTPError:
        return None
    return {"passed": d.get("passCount", 0), "failed": d.get("failCount", 0),
            "skipped": d.get("skipCount", 0), "total": d.get("totalCount", 0)}


def failed_cases(job: str, number: int, limit: int = 20) -> list[dict]:
    """The failing test cases (name + error) so the agent can self-heal or report them."""
    jp = _base() + _job_path(job)
    try:
        d = _json(f"{jp}/{number}/testReport/api/json")
    except urllib.error.HTTPError:
        return []
    out = []
    for suite in d.get("suites", []):
        for case in suite.get("cases", []):
            if case.get("status") in ("FAILED", "REGRESSION"):
                # Playwright's JUnit sets className to the spec file path — keep it so self-heal
                # can fetch and repair exactly that file.
                out.append({"test": f"{case.get('className','')}.{case.get('name','')}",
                            "file": case.get("className", ""),
                            "error": (case.get("errorDetails") or "").strip()[:400]})
                if len(out) >= limit:
                    return out
    return out


def console_tail(job: str, number: int, lines: int = 40) -> str:
    jp = _base() + _job_path(job)
    try:
        _, _, raw = _req("GET", f"{jp}/{number}/consoleText")
        return "\n".join(raw.decode(errors="ignore").splitlines()[-lines:])
    except Exception:
        return ""


def build_url(job: str, number: int) -> str:
    return f"{_base()}{_job_path(job)}/{number}/"


def run_job(job: str, params: dict | None = None,
            start_timeout: int = 120, run_timeout: int = 1800) -> dict:
    """End-to-end: trigger a job, wait for it, and return a structured report.
    Returns {job, number, url, result, report, failures, console}."""
    queue_url = trigger(job, params)
    number = await_build_number(queue_url, timeout=start_timeout)
    result = await_result(job, number, timeout=run_timeout)
    report = test_report(job, number)
    return {"job": job, "number": number, "url": build_url(job, number), "result": result,
            "report": report, "failures": failed_cases(job, number),
            "console": console_tail(job, number) if result != "SUCCESS" else ""}
