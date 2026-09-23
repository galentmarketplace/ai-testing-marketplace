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
import json
import os
import re
import urllib.error
import urllib.request

from . import runctx

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


def _provider() -> str:
    return os.environ.get("LLM_PROVIDER", "anthropic").lower()


def _call_anthropic(system: str, user: str, max_tokens: int) -> str:
    import anthropic  # lazy: only needed for this provider
    client = anthropic.Anthropic()
    kwargs = dict(model=os.environ.get("CLAUDE_MODEL", "claude-sonnet-5"),
                  max_tokens=max_tokens, system=system,
                  messages=[{"role": "user", "content": user}])
    resp = client.messages.create(**kwargs)   # newer models reject temperature — omit it
    # concatenate any text blocks (skip tool/thinking blocks)
    return "".join(getattr(b, "text", "") for b in resp.content) or resp.content[0].text


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
            return data["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < 4:
                retry_after = e.headers.get("Retry-After") if e.headers else None
                wait = float(retry_after) if (retry_after and retry_after.replace('.', '', 1).isdigit()) else (2 ** attempt) * 2
                print(f"  [LLM] {provider} {e.code} — backing off {wait:.0f}s (attempt {attempt + 1}/5)")
                _time.sleep(min(wait, 30))
                continue
            raise


def call_llm(agent_name: str, system: str, user: str, max_tokens: int = 4000) -> str:
    """Return the raw text of the model response (routed to the configured provider)."""
    if runctx.is_mock():
        return _mock_response(agent_name)
    p = _provider()
    if p == "anthropic":
        return _call_anthropic(system, user, max_tokens)
    if p in _OPENAI_COMPAT:
        return _call_openai_compatible(p, system, user, max_tokens)
    raise ValueError(f"Unknown LLM_PROVIDER '{p}'. Use one of: anthropic, "
                     + ", ".join(_OPENAI_COMPAT))


def call_llm_json(agent_name: str, system: str, user: str, max_tokens: int = 4000) -> dict:
    """Call the LLM and parse a JSON object out of the response."""
    text = call_llm(agent_name, system, user, max_tokens=max_tokens)
    match = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)  # strip code fences
    if match:
        text = match.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"[{agent_name}] No JSON object in LLM response:\n{text[:500]}")
    return json.loads(text[start:end + 1])
