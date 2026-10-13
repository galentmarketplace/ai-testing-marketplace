"""Data Analyst agent — the model writes the analysis code, and the CODE produces the answer.

This is the Julius-AI mechanism: point it at a data file, ask a question in plain English,
and it writes Python, runs it, and reports what the run printed. It is deliberately the
opposite of the Insights agent, which computes figures in our code and lets the model only
write prose about them. Here the model writes the arithmetic — so the arithmetic has to be
executed, never trusted:

  * the answer is ONLY the script's own stdout and the files it wrote. The model is never
    asked to summarise, interpret or restate a result into numbers after the fact.
  * a script that raises is a failure, not an answer. The traceback goes back to the model
    for a bounded number of repair attempts, and if it still fails the agent reports the
    traceback — the same rule the CI healer follows.
  * the executed source ships as an artifact, so every figure can be traced to the code
    that produced it.

Execution posture (model-written code over user-supplied data):
  * `python -I` — isolated mode, so neither the current directory nor user site-packages can
    shadow a stdlib import. A file named `json.py` sitting beside the data cannot hijack it.
  * a fresh per-run working directory; the data file is COPIED in, so the original cannot be
    modified by the script.
  * `sandbox.child_env()` — no secrets reach the child; MPLBACKEND=Agg keeps matplotlib headless.
  * a hard timeout, and output caps on what is read back.

What is NOT enforced, and is worth being honest about: the script runs on the server's own
interpreter, so its site-packages are importable and the network is reachable. The prompt
forbids both, but a prompt is not a sandbox. Confining this properly needs a dedicated
environment or a container — fine for a single-user deployment, not for running other
people's questions.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

from .. import runctx, sandbox
from ..llm import call_llm_json
from ..mocks import MOCK_RESPONSES
from ..state import GateDecision, PipelineState

MAX_ATTEMPTS = int(os.environ.get("ATM_ANALYST_ATTEMPTS", "3"))
TIMEOUT_S = int(os.environ.get("ATM_ANALYST_TIMEOUT", "120"))
_MAX_OUT = 12000

MOCK_RESPONSES.setdefault("data_analyst_agent", json.dumps({
    "approach": "Load the CSV, count runs by status, and chart the duration distribution.",
    "code": "import sys, pandas as pd\n"
            "df = pd.read_csv(sys.argv[1])\n"
            "print('rows:', len(df))\n"
            "print(df['status'].value_counts().to_string())\n"}))

SYSTEM = """You are a data analyst. You write a SINGLE self-contained Python script that answers
the user's question about a data file, and you write nothing else.

The script:
- receives the data file path as sys.argv[1]. Never hard-code a path.
- may use pandas, matplotlib (headless), numpy and the standard library. Nothing else.
- PRINTS its findings as plain text. Everything you want the reader to know must be printed:
  the script's stdout IS the answer, and nothing you write outside the code is reported.
- saves any chart with plt.savefig('<name>.png', dpi=140, bbox_inches='tight') in the CURRENT
  directory. Give each chart a title and axis labels. Never call plt.show().
- must not access the network, read any other file, or write outside the current directory.
- must handle the real shape of the data: inspect columns before using them, and if a column
  the question implies is absent, print that plainly instead of inventing a substitute.

Print the numbers you were asked for, not a narrative. Label every printed figure.
Respond with ONLY JSON: {"approach":"<one sentence>","code":"<the full script>"}"""

REPAIR = """Your script failed. Here is the traceback. Return a corrected, complete script.
Fix the actual cause — do not wrap the failure in try/except to make it pass silently, and do
not print a result the code did not compute.
Respond with ONLY JSON: {"approach":"<what you changed>","code":"<the full script>"}"""

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")


def _history_csv(dest: Path, limit: int) -> tuple[Path | None, str]:
    """Zero-config data source: this platform's own run history, exported as a CSV.

    Shares `analytics.fold_run` with the Insights agent, so there is one definition of what
    a run's facts are — including its provenance, which the exported rows carry.
    """
    from ..integration import analytics
    try:
        from web import store  # noqa: PLC0415 — optional, layer-crossing
    except Exception:
        return None, "no run store is reachable from here"
    try:
        rows = []
        for row in reversed(store.list_runs(None, limit=limit, admin=True)):
            full = store.get_run(row["id"]) or row
            r = analytics.fold_run(full, store.events_after(row["id"], 0))
            cov = r.get("coverage") or {}
            perf = r.get("perf") or {}
            sec = r.get("security") or {}
            rows.append({
                "run": r["num"], "flow": r["flow"], "status": r["status"],
                "simulated": {True: "yes", False: "no"}.get(r.get("mock"), "unknown"),
                "duration_s": r.get("duration_s"), "cost_usd": r.get("cost_usd"),
                "self_corrections": r.get("self_corrections"), "agents_run": r.get("agents_run"),
                "tracks": " ".join(r.get("tracks") or []),
                "gates_passed": sum(1 for v in (r.get("gates") or {}).values() if v == "pass"),
                "gates_failed": sum(1 for v in (r.get("gates") or {}).values() if v != "pass"),
                "repo": r.get("repo"),
                "coverage_before_pct": cov.get("before"), "coverage_after_pct": cov.get("after"),
                "p95_ms": perf.get("p95_ms"), "p99_ms": perf.get("p99_ms"),
                "error_rate": perf.get("error_rate"),
                "security_findings": sec.get("total"),
            })
    except Exception as exc:
        return None, f"could not read the run history ({exc})"
    if not rows:
        return None, "the run history is empty"
    import csv
    out = dest / "run-history.csv"
    with out.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    return out, f"this platform's own run history ({len(rows)} runs)"


def _fetch(source: str, dest: Path) -> tuple[Path | None, str]:
    """Bring the data in. A local path is COPIED so the script cannot modify the original."""
    if source.startswith(("http://", "https://")):
        name = _SAFE_NAME.sub("_", Path(urllib.parse.urlparse(source).path).name) or "data.csv"
        out = dest / name
        try:
            req = urllib.request.Request(source, headers={"User-Agent": "atm-data-analyst"})
            with urllib.request.urlopen(req, timeout=60) as r:   # noqa: S310 — user-supplied URL
                out.write_bytes(r.read(50 * 1024 * 1024))
        except Exception as exc:
            return None, f"could not download {source}: {exc}"
        return out, source
    src = Path(source).expanduser()
    if not src.is_file():
        return None, f"no such file: {source}"
    out = dest / _SAFE_NAME.sub("_", src.name)
    shutil.copy2(src, out)
    return out, str(src)


def _profile(path: Path) -> str:
    """What the model is told about the data: real columns and real sample rows, never guessed."""
    try:
        import pandas as pd
        df = (pd.read_excel(path) if path.suffix.lower() in (".xlsx", ".xls")
              else pd.read_json(path) if path.suffix.lower() == ".json"
              else pd.read_csv(path))
    except Exception as exc:
        head = path.read_text(errors="ignore")[:1500] if path.exists() else ""
        return f"(could not parse with pandas: {exc})\nFirst bytes:\n{head}"
    cols = "\n".join(f"  - {c} ({df[c].dtype}), e.g. {df[c].dropna().head(2).tolist()}"
                     for c in df.columns[:40])
    head = (f"{len(df)} rows x {len(df.columns)} columns\nColumns:\n{cols}\n\n"
            f"First rows:\n{df.head(5).to_string()}")
    # pandas does not raise on a file that is not a table — it returns an empty frame with a
    # placeholder column. Saying "0 rows" and nothing else invites a script written against a
    # shape that does not exist, so show the raw bytes and let the model see the truth.
    if len(df) == 0 or all(str(c).startswith("Unnamed") for c in df.columns):
        raw = path.read_text(errors="ignore")[:800]
        head += ("\n\n(this did not parse as a table — pandas found no usable rows or headers)"
                 f"\nFirst bytes:\n{raw}")
    return head


def _execute(code: str, work: Path, data: Path) -> dict:
    """Run the script in isolated mode. Its stdout is the answer; a traceback is a failure."""
    script = work / "analysis.py"
    script.write_text(code)
    before = {p.name for p in work.iterdir()}
    try:
        proc = subprocess.run(
            [sys.executable, "-I", str(script), str(data.name)],
            cwd=str(work), capture_output=True, text=True, timeout=TIMEOUT_S,
            env=sandbox.child_env({"MPLBACKEND": "Agg", "HOME": str(work),
                                   "MPLCONFIGDIR": str(work / ".mpl")}))
    except subprocess.TimeoutExpired:
        return {"ok": False, "stdout": "", "error": f"the script did not finish within {TIMEOUT_S}s"}
    except Exception as exc:
        return {"ok": False, "stdout": "", "error": f"could not run the script: {exc}"}
    charts = sorted(p.name for p in work.iterdir()
                    if p.suffix.lower() == ".png" and p.name not in before)
    return {"ok": proc.returncode == 0, "stdout": (proc.stdout or "")[-_MAX_OUT:],
            "error": (proc.stderr or "")[-4000:] if proc.returncode else "",
            "charts": charts, "exit": proc.returncode}


def analyze_data(state: PipelineState) -> dict:
    inp = state.get("story", {}).get("inputs", {}) or {}
    question = (inp.get("question") or "").strip() or "Summarise this data: what stands out?"
    source = (inp.get("data_source") or "").strip()
    try:
        limit = max(1, min(500, int(str(inp.get("history_limit") or "100").strip() or 100)))
    except ValueError:
        limit = 100

    work = sandbox.run_workspace("analysis")
    work.mkdir(parents=True, exist_ok=True)
    arts = list(state.get("analysis_artifacts", []))

    def fail(msg: str) -> dict:
        print(f"  [Data Analyst] {msg}")
        (work / "analysis-report.md").write_text(f"# Data analysis\n\n**Not analysed:** {msg}\n")
        arts.append({"type": "analysis-report", "path": str(work / "analysis-report.md"),
                     "tags": ["@analysis"]})
        return {"analysis_artifacts": arts,
                "analysis_report": {"ok": False, "error": msg, "question": question}}

    try:
        import pandas  # noqa: F401
    except ImportError:
        return fail("pandas is not installed in this environment — "
                    "install it with `pip install pandas matplotlib openpyxl`")

    if runctx.is_mock():
        return {"analysis_artifacts": arts, "analysis_report": {
            "ok": True, "mock": True, "question": question, "source": "(mock run)",
            "approach": "mock run — no script was executed",
            "code": "", "stdout": "(mock run — nothing was executed)",
            "charts": [], "attempts": 0, "data_file": "(none)"}}

    data, origin = (_fetch(source, work) if source else _history_csv(work, limit))
    if data is None:
        return fail(origin)
    print(f"  [Data Analyst] data: {origin} → {data.name}")

    profile = _profile(data)
    base_user = (f"QUESTION: {question}\n\nDATA FILE: {data.name} (passed as sys.argv[1])\n"
                 f"SHAPE AND REAL COLUMNS:\n{profile}\n")

    result, code, approach, history = None, "", "", []
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            if attempt == 1:
                raw = call_llm_json("data_analyst_agent", SYSTEM, base_user,
                                    max_tokens=int(os.environ.get("ATM_ANALYST_MAX_TOKENS", "16000")))
            else:
                raw = call_llm_json("data_analyst_agent", SYSTEM + "\n\n" + REPAIR,
                                    base_user + f"\n\nYOUR PREVIOUS SCRIPT:\n{code}\n\n"
                                                f"TRACEBACK:\n{result['error']}",
                                    max_tokens=int(os.environ.get("ATM_ANALYST_MAX_TOKENS", "16000")))
        except Exception as exc:
            return fail(f"the analysis could not be generated: {exc}")
        code, approach = raw.get("code") or "", raw.get("approach") or ""
        if not code.strip():
            return fail("the model returned no script")
        result = _execute(code, work, data)
        history.append({"attempt": attempt, "ok": result["ok"],
                        "error": (result.get("error") or "").strip()[-400:]})
        if result["ok"]:
            print(f"  [Data Analyst] script ran on attempt {attempt} — "
                  f"{len(result['charts'])} chart(s), {len(result['stdout'].splitlines())} line(s) of output")
            break
        print(f"  [Data Analyst] attempt {attempt}/{MAX_ATTEMPTS} failed: "
              f"{(result.get('error') or '').strip().splitlines()[-1][:160] if result.get('error') else 'no output'}")

    (work / "analysis.py").write_text(code)
    arts.append({"type": "analysis-code", "path": str(work / "analysis.py"), "tags": ["@analysis"]})
    for c in result.get("charts", []):
        arts.append({"type": "analysis-chart", "path": str(work / c), "tags": ["@analysis", "@chart"]})

    report = {"ok": bool(result and result["ok"]), "question": question, "source": origin,
              "data_file": data.name, "approach": approach, "code": code,
              "stdout": result.get("stdout", "") if result else "",
              "error": result.get("error", "") if result else "",
              "charts": [str(work / c) for c in result.get("charts", [])] if result else [],
              "attempts": len(history), "attempt_log": history,
              "profile": profile.splitlines()[0] if profile else ""}
    md = _report_markdown(report)
    (work / "analysis-report.md").write_text(md)
    arts.append({"type": "analysis-report", "path": str(work / "analysis-report.md"),
                 "tags": ["@analysis"]})
    if not report["ok"]:
        print(f"  [Data Analyst] FAILED after {len(history)} attempt(s) — reporting the traceback, "
              f"not an answer")
    return {"analysis_artifacts": arts, "analysis_report": report}


def _report_markdown(r: dict) -> str:
    L = [f"# Data analysis — {r['data_file']}", "", f"**Question:** {r['question']}", "",
         f"**Source:** {r['source']}", ""]
    if r.get("approach"):
        L += [f"**Approach:** {r['approach']}", ""]
    if r["ok"]:
        L += ["## Result", "",
              "_Everything below is the script's own output. Nothing here was written by the "
              "model after the fact._", "", "```", r["stdout"].rstrip() or "(no output)", "```", ""]
    else:
        L += ["## Not answered", "",
              f"The script failed on all {r['attempts']} attempt(s), so there is no result. "
              f"The traceback is reported instead of an answer:", "",
              "```", (r.get("error") or "no output").strip(), "```", ""]
    if r.get("charts"):
        L += ["## Charts", ""] + [f"- `{Path(c).name}`" for c in r["charts"]] + [""]
    if r["attempts"] > 1:
        L += [f"_Took {r['attempts']} attempt(s); each failure's traceback was fed back for repair._", ""]
    L += ["## The code that produced this", "", "```python", r["code"].rstrip(), "```"]
    return "\n".join(L)


def analysis_gate(state: PipelineState) -> dict:
    """Advisory. The question is not 'is the answer good' but 'did a script produce it'."""
    r = state.get("analysis_report", {}) or {}
    if not r.get("ok"):
        reason = f"No answer: {r.get('error') or 'the analysis script never ran successfully'}"
        checks = [{"label": "the analysis script did not run to completion",
                   "threshold": "answer must come from execution", "ok": False},
                  {"label": f"{r.get('attempts', 0)} attempt(s) made, each traceback fed back",
                   "threshold": f"≤ {MAX_ATTEMPTS}", "ok": True, "advisory": True}]
    else:
        lines = len((r.get("stdout") or "").strip().splitlines())
        reason = (f"Answered by executing a generated script on attempt {r['attempts']} — "
                  f"{lines} line(s) of computed output, {len(r.get('charts') or [])} chart(s).")
        checks = [
            {"label": f"script executed successfully (attempt {r['attempts']} of {MAX_ATTEMPTS})",
             "threshold": "exit 0", "ok": True},
            {"label": f"{lines} line(s) of output came from the script, not the model",
             "threshold": "answer is computed", "ok": lines > 0},
            {"label": f"{len(r.get('charts') or [])} chart(s) written by the script",
             "threshold": "visual evidence", "ok": True, "advisory": True},
            {"label": "the executed source is attached", "threshold": "auditable", "ok": bool(r.get("code"))},
        ]
    decision = GateDecision(gate="ANALYSIS", verdict="pass" if r.get("ok") else "fail",
                            reason=reason, checks=checks, route_to=None)
    print(f"  [Gate ANALYSIS] {decision.verdict.upper()} — {reason}")
    return {"gate_decisions": state.get("gate_decisions", []) + [decision.model_dump()]}
