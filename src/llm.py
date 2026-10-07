"""LLM client wrapper — provider-agnostic.

- MOCK_LLM=1        → canned responses (no key, no cost).
- LLM_PROVIDER=...  → pick a backend:
    anthropic   (default)  ANTHROPIC_API_KEY, CLAUDE_MODEL
    groq                   GROQ_API_KEY        (free key, hosts open Llama models)
    ollama                 —                    (fully local & open-source, no key)
    openai                 OPENAI_API_KEY
    openrouter             OPENROUTER_API_KEY   (has free models)
  Any non-anthropic provider is called via the OpenAI-compatible /chat/completions
  API, so Groq, Ollama, OpenRouter, Together, etc. all work with one adapter.

Override the endpoint/model with LLM_BASE_URL and LLM_MODEL when needed.

Every agent asks for JSON and we parse strictly — structured output is what makes
multi-agent handoffs reliable.
"""
import contextvars
import json
import os
import re
import urllib.error
import urllib.request

from . import observability, runctx

# Which agent is calling — so token usage is attributed per agent within a run.
_AGENT: contextvars.ContextVar[str] = contextvars.ContextVar("atm_llm_agent", default="")

# provider -> (base_url, api-key env var or None, default model)
_OPENAI_COMPAT = {
    "groq":       ("https://api.groq.com/openai/v1", "GROQ_API_KEY", "llama-3.3-70b-versatile"),
    "ollama":     ("http://localhost:11434/v1", None, "llama3.1"),
    "openai":     ("https://api.openai.com/v1", "OPENAI_API_KEY", "gpt-4o-mini"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY",
                   "meta-llama/llama-3.3-70b-instruct:free"),
}


def _mock_response(agent_name: str) -> str:
    from .mocks import MOCK_RESPONSES
    return MOCK_RESPONSES[agent_name]


class TruncatedResponse(RuntimeError):
    """The model stopped because it ran out of output budget, so the reply is incomplete."""


def _provider() -> str:
    return os.environ.get("LLM_PROVIDER", "anthropic").lower()


# Above this output budget the SDK refuses a non-streaming request outright ("Streaming is
# required for operations that may take longer than 10 minutes"), so a large budget must
# stream. Agents that write whole spec files need those budgets.
_STREAM_ABOVE = 8192


def _call_anthropic(system: str, user: str, max_tokens: int) -> str:
    import anthropic  # lazy: only needed for this provider
    client = anthropic.Anthropic()
    kwargs = dict(model=os.environ.get("CLAUDE_MODEL", "claude-sonnet-5"),
                  max_tokens=max_tokens, system=system,
                  messages=[{"role": "user", "content": user}])
    if max_tokens > _STREAM_ABOVE:
        with client.messages.stream(**kwargs) as stream:
            resp = stream.get_final_message()
    else:
        resp = client.messages.create(**kwargs)   # newer models reject temperature — omit it
    u = getattr(resp, "usage", None)          # per-run token + cost accounting
    if u is not None:
        observability.record_usage(_AGENT.get() or "llm", kwargs["model"],
                                   int(getattr(u, "input_tokens", 0) or 0),
                                   int(getattr(u, "output_tokens", 0) or 0))
    # concatenate any text blocks (skip tool/thinking blocks)
    text = "".join(getattr(b, "text", "") for b in resp.content) or resp.content[0].text
    # A response cut off at the token ceiling is a HALF-WRITTEN document, not a bad one.
    # Reporting it as "empty output" sends the caller looking for the wrong problem, and a
    # retry at the same budget fails identically. Say what actually happened.
    if getattr(resp, "stop_reason", None) == "max_tokens":
        raise TruncatedResponse(
            f"the model hit the {max_tokens}-token output ceiling and the reply is "
            f"incomplete ({len(text)} chars). Raise max_tokens or narrow the request.")
    return text


def _call_openai_compatible(provider: str, system: str, user: str, max_tokens: int) -> str:
    base, key_env, default_model = _OPENAI_COMPAT[provider]
    base = os.environ.get("LLM_BASE_URL", base).rstrip("/")
    model = os.environ.get("LLM_MODEL", default_model)
    # A real User-Agent is required — Groq/others sit behind Cloudflare, which 403s "Python-urllib".
    headers = {"Content-Type": "application/json", "User-Agent": "agentic-testing-pipeline/1.0"}
    if key_env:
        key = os.environ.get(key_env) or os.environ.get("LLM_API_KEY", "")
        if key:
            headers["Authorization"] = f"Bearer {key}"
    payload = {
        "model": model, "temperature": 0, "max_tokens": max_tokens,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
    }
    # gpt-oss / reasoning models spend tokens on hidden reasoning before the answer —
    # keep it low so the actual (often large) output isn't truncated to empty.
    if "gpt-oss" in model or "reasoning" in model:
        payload["reasoning_effort"] = "low"
    req = urllib.request.Request(f"{base}/chat/completions",
                                 data=json.dumps(payload).encode(), headers=headers)
    # Free tiers (Groq) rate-limit aggressively → retry 429/5xx with exponential back-off,
    # honouring Retry-After when present, so the self-heal loop can actually converge.
    import time as _time
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                data = json.loads(r.read())
            u = data.get("usage") or {}
            if u:
                observability.record_usage(_AGENT.get() or "llm", model,
                                           int(u.get("prompt_tokens", 0)), int(u.get("completion_tokens", 0)))
            return data["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < 4:
                retry_after = e.headers.get("Retry-After") if e.headers else None
                wait = float(retry_after) if (retry_after and retry_after.replace('.', '', 1).isdigit()) else (2 ** attempt) * 2
                print(f"  [LLM] {provider} {e.code} — backing off {wait:.0f}s (attempt {attempt + 1}/5)")
                _time.sleep(min(wait, 30))
                continue
            raise


# 4000 truncated the perf agent mid-script and blocked a run. Every agent here writes a
# file, a script or a case list, so the floor is what a generated FILE needs, not a reply.
DEFAULT_MAX_TOKENS = int(os.environ.get("ATM_DEFAULT_MAX_TOKENS", "16000"))


def call_llm(agent_name: str, system: str, user: str,
             max_tokens: int = DEFAULT_MAX_TOKENS) -> str:
    """Return the raw text of the model response (routed to the configured provider)."""
    _AGENT.set(agent_name)
    if runctx.is_mock():
        return _mock_response(agent_name)
    p = _provider()
    if p == "anthropic":
        return _call_anthropic(system, user, max_tokens)
    if p in _OPENAI_COMPAT:
        return _call_openai_compatible(p, system, user, max_tokens)
    raise ValueError(f"Unknown LLM_PROVIDER '{p}'. Use one of: anthropic, "
                     + ", ".join(_OPENAI_COMPAT))


def _json_objects(text: str) -> list[dict]:
    """Every complete top-level JSON object in the text, in order.

    Taking everything between the first `{` and the last `}` fails the moment a model emits
    anything after its answer — a second fenced block, a closing remark, a repeated object.
    That produced `Extra data: line 3 column 1`, which discarded a perfectly good reply: the
    Go test generator lost its tests on roughly one run in three and the coverage fix
    silently did not happen.
    """
    dec = json.JSONDecoder()
    out, i, n = [], 0, len(text)
    while i < n:
        i = text.find("{", i)
        if i == -1:
            break
        try:
            obj, end = dec.raw_decode(text, i)
        except json.JSONDecodeError:
            i += 1
            continue
        if isinstance(obj, dict):
            out.append(obj)
        i = max(end, i + 1)
    return out


def call_llm_json(agent_name: str, system: str, user: str,
                  max_tokens: int = DEFAULT_MAX_TOKENS) -> dict:
    """Call the LLM and parse a JSON object out of the response."""
    text = call_llm(agent_name, system, user, max_tokens=max_tokens)
    fenced = re.findall(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    candidates = _json_objects("\n".join(fenced)) if fenced else []
    if not candidates:
        candidates = _json_objects(text)
    # A top-level array where an object was asked for is a SHAPE error, not stray text.
    # Lifting the first element out of it would hand the caller a plausible-looking dict
    # that is missing the keys it needs.
    body = ("\n".join(fenced) if fenced else text).strip()
    if body.startswith("["):
        try:
            if isinstance(json.loads(body), list):
                raise ValueError(f"[{agent_name}] the model returned a JSON array, "
                                 f"not the requested object")
        except json.JSONDecodeError:
            pass
    if candidates:
        if len(candidates) > 1:
            # Keep the richest object; a preamble or a sign-off is never the answer.
            candidates.sort(key=lambda o: len(json.dumps(o)), reverse=True)
            print(f"  [{agent_name}] the model returned {len(candidates)} JSON objects — "
                  f"using the largest ({len(candidates[0])} key(s)) and ignoring the rest")
        return candidates[0]
    if "{" not in text:
        raise ValueError(f"[{agent_name}] No JSON object in LLM response:\n{text[:500]}")
    # Nothing decoded: an unterminated document almost always means a cut-off reply.
    hint = (" — the response looks cut off, so the output budget is probably too small"
            if not text.rstrip().endswith("}") else "")
    raise ValueError(f"[{agent_name}] malformed JSON from the model{hint}: "
                     f"no complete object in {len(text)} chars")
