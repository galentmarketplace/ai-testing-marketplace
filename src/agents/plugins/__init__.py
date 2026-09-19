"""Drop-in agent plugins.

Any module here that exposes a module-level ``SPECS`` list of ``AgentSpec`` is
auto-registered by ``src/registry.py`` at import time. Adding a new marketplace
capability is therefore a matter of dropping ONE file in this folder — no edits
to the registry, orchestrator, web server, or UI.
"""
