"""Prescreen Agent — the intake stage that decides a run's *scope*.

This runs FIRST, before any build/test work, and emits a RunConfig telling the
orchestrator which tracks to activate. Not every change needs every agent: a
regression-only request should trigger the regression loop and nothing else.

Config is supplied EXPLICITLY by the caller (web UI / CLI) as a raw dict, e.g.
`{"mode": "regression"}`. The prescreen expands that `mode` into concrete
tracks + entry point + ship flag via the table below. Callers can also pass an
explicit `tracks` list (mode="custom") to hand-pick the scope.

(The real product could add an LLM-inferred mode here — read the diff and pick
tracks itself — but explicit intake is deterministic and testable first.)
"""
from ..state import ALL_TRACKS, PipelineState, RunConfig

# mode -> (tracks, entry, open_pr). "custom" is handled specially (uses caller's tracks).
# Only "full" builds a feature from a story (AC -> Dev -> tests -> PR). Every other scope
# runs its test agents INDEPENDENTLY against an existing repo — no code generation.
_MODE_TABLE = {
    "full":       (list(ALL_TRACKS),            "from_story",         True),
    "smoke":      (["unit", "functional"],      "from_existing_code", False),
    "perf":       (["perf"],                    "from_existing_code", False),
    "regression": (["regression"],              "from_existing_code", False),
}


def _resolve(raw: dict) -> RunConfig:
    """Expand a raw explicit config dict into a fully-resolved RunConfig."""
    mode = raw.get("mode", "full")

    if mode == "custom":
        # Custom defaults to testing an existing repo — each agent runs independently.
        # Pass entry="from_story" (+ the build tracks) only to generate a feature.
        tracks = raw.get("tracks") or list(ALL_TRACKS)
        entry = raw.get("entry", "from_existing_code")
        open_pr = raw.get("open_pr", False)
    else:
        tracks, entry, open_pr = _MODE_TABLE.get(mode, _MODE_TABLE["full"])
        tracks = list(tracks)
        if mode == "full":
            # "full" = every track the registry currently offers, so a newly-registered
            # agent joins full runs automatically (lazy import avoids a registry↔prescreen cycle).
            from ..registry import ALL_TRACKS as REGISTRY_TRACKS
            # 'jenkins' is opt-in (functional/regression/custom flows), not part of the local
            # build-a-feature pipeline — otherwise it would supersede full's local gates.
            tracks = [t for t in REGISTRY_TRACKS if t != "jenkins"]
        # A caller may still override individual fields on top of a named mode.
        if raw.get("tracks"):
            tracks = list(raw["tracks"])
        entry = raw.get("entry", entry)
        open_pr = raw.get("open_pr", open_pr)

    if "build" in tracks and "criteria" not in tracks:
        tracks = tracks + ["criteria"]           # Dev needs parsed criteria to build against

    reason = raw.get("reason") or (
        f"mode={mode}: activating {', '.join(tracks)}; "
        f"entry={entry}; {'opens PR' if open_pr else 'no PR'}."
    )
    return RunConfig(mode=mode, tracks=tracks, entry=entry, open_pr=open_pr,
                     reason=reason, _resolved=True)


def prescreen(state: PipelineState) -> dict:
    raw = state.get("run_config") or {}
    cfg = _resolve(raw)
    print(f"  [Prescreen] {cfg.mode} -> tracks={cfg.tracks} entry={cfg.entry} pr={cfg.open_pr}")
    # Preserve transient, non-RunConfig keys the server attached (e.g. the Jira connection used to store
    # cases / results back on the ticket, the GitHub token) — resolved fields win, extras survive.
    resolved = cfg.model_dump(by_alias=True)
    extras = {k: v for k, v in raw.items() if k not in resolved and k not in ("mode", "tracks", "entry", "open_pr")}
    return {"run_config": {**extras, **resolved}}
