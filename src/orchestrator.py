"""Autonomous orchestrator — a generic, registry-driven dependency scheduler.

The orchestrator no longer hard-codes the pipeline. It reads the Agent Registry
(`registry.py`) and runs a single generic loop:

    prescreen -> resolve which tracks are active
    loop:
        pick the next runnable step
            (track active, every in-plan dependency done, not yet done)
        dispatch it, merge its result into state
        if it was a gate that FAILED: reset its on_fail_reset steps to pending
            (fixer + chain + gate) and burn one retry; block when retries run out
    until nothing is runnable (goal reached) or a gate blocks.

Because ordering comes from each agent's declared `depends_on` — not an if-ladder —
adding a new agent to the registry makes it schedule automatically. The agents
themselves still call Claude; the orchestrator is deterministic infrastructure,
which is what a platform that "just executes" whatever agents are installed needs.

Subagents never call each other — they only read/write PipelineState.
"""
import time
from collections.abc import Iterator

from .agents.prescreen_agent import prescreen
from .registry import BY_ID, plan_for

MAX_ITERS = 60  # runaway backstop, not a quality gate


def _latest_verdict(state: dict, gate_name: str) -> str:
    for g in reversed(state.get("gate_decisions", [])):
        if g["gate"] == gate_name:
            return g["verdict"]
    return ""


def _reason(spec, note: str) -> str:
    """One-line narration for a dispatch (prefixed with any loop-back note)."""
    kind = {"agent": "Dispatch", "runner": "Run", "gate": "Evaluate"}.get(spec.kind, "Run")
    base = f"{kind} {spec.label} — {spec.produces}."
    return f"{note} {base}".strip() if note else base


class RunCancelled(Exception):
    """Raised inside the loop when a cancel was requested or the run deadline passed."""


def orchestrate(story: dict, config: dict | None = None, max_iters: int = MAX_ITERS,
                should_cancel=None, deadline_s: float | None = None) -> Iterator[dict]:
    """Run the scheduler, yielding events:
      {"type":"think", reasoning, next, iteration}
      {"type":"node",  node, label, kind, update}
      {"type":"done",  status}

    `config` is the raw intake (e.g. {"mode": "regression"}); the Prescreen step
    resolves it into the active tracks before the plan is built.
    """
    config = config or {"mode": "full"}
    # bind this user's GitHub token for the whole run (repo scan + PR publishing)
    from .integration import github as _gh
    _gh.set_token(config.get("github_token"))
    state: dict = {"story": story, "status": "running", "attempts": {},
                   "run_config": config}
    it = 0
    started = time.monotonic()

    def _check_abort() -> str | None:
        """Cooperative stop between steps: an operator cancel, or the whole-run deadline."""
        if should_cancel is not None and should_cancel():
            return "cancelled"
        if deadline_s is not None and (time.monotonic() - started) > deadline_s:
            return "timed_out"
        return None

    def node_event(spec, update):
        return {"type": "node", "node": spec.id, "label": spec.label,
                "kind": spec.kind, "update": update}

    # ---- Step 0: Prescreen always runs first and resolves the scope ----
    it += 1
    yield {"type": "think", "iteration": it,
           "reasoning": "New run in — prescreen the request to decide which tracks to activate.",
           "next": "prescreen"}
    update = prescreen(state)
    state = {**state, **update}
    yield node_event(BY_ID["prescreen"], update)

    # ---- Real-world integration: clone & analyze the target repo so the agents
    # generate against the ACTUAL endpoints / pages / models (deterministic, no LLM). ----
    repo = (state.get("story") or {}).get("inputs", {}).get("repo")
    if repo:
        from .integration.repo_analyzer import analyze_repo
        it += 1
        yield {"type": "think", "iteration": it, "next": "analyze_repo",
               "reasoning": f"Cloning & analyzing {repo} …"}
        analysis = analyze_repo(repo)
        state["repo_analysis"] = analysis
        note = (f"Repo analyzed — {analysis['stack']}: {len(analysis['entities'])} entities, "
                f"{analysis['endpoint_count']} endpoints, {len(analysis['ui_routes'])} UI routes"
                + (f", login route {analysis['login_route']}" if analysis.get("login_route") else "") + "."
                if analysis.get("ok") else f"Repo analysis skipped: {analysis.get('error')}")
        yield {"type": "think", "iteration": it, "next": "prescreen", "reasoning": note}
        # surface the code-derived analysis to the UI (Sources-of-truth panel)
        yield {"type": "node", "node": "analyze_repo", "label": "Repo Analysis", "kind": "agent",
               "update": {"repo_analysis": analysis}}

    cfg = state["run_config"]
    plan = plan_for(cfg)
    in_plan = {s.id for s in plan}
    # trigger_only steps (Self-Heal) start 'done' so the forward flow skips them;
    # a gate loop-back resets them to 'pending' to fire them.
    status = {s.id: ("done" if s.trigger_only else "pending") for s in plan}
    retries: dict[str, int] = {}
    priority: set[str] = set()   # a failed gate's fix-loop — resolve it before unrelated work
    errors: list[dict] = []      # steps that crashed (run blocks; never silently "done")
    note = ""

    def _runnable(s) -> bool:
        if status[s.id] != "pending":
            return False
        ok = all(status[d] == "done" for d in s.depends_on if d in in_plan)
        # push_branch (ship) waits on every in-plan gate EXCEPT post-PR (CI) gates, so it can
        # commit the branch Jenkins runs against without deadlocking on Jenkins itself.
        if s.ship:
            ok = ok and all(status[g.id] == "done" for g in plan
                            if g.kind == "gate" and not g.post_pr)
        # open_pr (final) is the true finish line: it waits on EVERY in-plan gate, including the
        # Jenkins CI gate, so the PR is opened only once everything is green.
        if s.final:
            ok = ok and all(status[g.id] == "done" for g in plan if g.kind == "gate")
        return ok

    while it < max_iters:
        it += 1
        stop = _check_abort()
        if stop:
            state["status"] = stop
            yield {"type": "think", "iteration": it, "next": "block",
                   "reasoning": ("Cancelled by the operator — stopping before the next step."
                                 if stop == "cancelled" else
                                 "Run deadline exceeded — stopping before the next step.")}
            break
        # A failing gate's fix-loop (heal -> re-run -> re-gate) takes priority, so a regression
        # failure is healed and re-checked BEFORE the orchestrator moves on to unrelated agents.
        spec = next((s for s in plan if s.id in priority and _runnable(s)), None) \
            or next((s for s in plan if _runnable(s)), None)

        if spec is None:  # nothing left to run — goal reached
            state["status"] = "done"
            yield {"type": "think", "iteration": it,
                   "reasoning": "Every in-scope agent and gate is green — goal reached.",
                   "next": "finish"}
            break

        yield {"type": "think", "iteration": it, "reasoning": _reason(spec, note), "next": spec.id}
        note = ""

        try:
            update = spec.fn(state)
            state = {**state, **update}
            status[spec.id] = "done"
            yield node_event(spec, update)
        except Exception as exc:
            # A crashed step is NOT a completed step. Mark it errored and block the run: letting the
            # plan continue produced "done" runs whose gate never ran (a silent green).
            status[spec.id] = "error"
            errors.append({"step": spec.id, "label": spec.label, "error": str(exc)})
            state["step_errors"] = list(errors)
            yield node_event(spec, {"error": str(exc), "step_status": "error"})
            state["status"] = "blocked"
            yield {"type": "think", "iteration": it, "next": "block",
                   "reasoning": f"{spec.label} crashed ({str(exc)[:160]}) — blocking for human triage "
                                f"rather than reporting a green run with a step that never produced output."}
            break

        # gate outcome: on failure, loop back (reset fixer + chain + gate) or block
        if spec.kind == "gate" and spec.gate_name:
            if _latest_verdict(state, spec.gate_name) == "fail":
                r = retries.get(spec.id, 0) + 1
                retries[spec.id] = r
                if r > spec.max_retries:
                    state["status"] = "blocked"
                    yield {"type": "think", "iteration": it,
                           "reasoning": f"{spec.gate_name} failed {r - 1}x — out of retries; block for human triage.",
                           "next": "block"}
                    break
                for sid in spec.on_fail_reset:
                    if sid in status:
                        status[sid] = "pending"
                        priority.add(sid)          # resolve this fix-loop before anything else
                note = f"{spec.gate_name} FAILED — self-correcting (attempt {r + 1})."
            else:                                  # gate passed — its fix-loop is resolved
                priority.difference_update(spec.on_fail_reset)

    yield {"type": "done", "status": state.get("status", "done")}
