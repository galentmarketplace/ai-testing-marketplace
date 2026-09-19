# Agentic Testing Pipeline — project guide

Multi-agent test orchestration prototype (LangGraph + Claude) with a live SSE web
dashboard. Python backend in `src/`, web UI in `web/` (`web/static/index.html`).

## Design work — ALWAYS use the ui-ux-pro-max skill

Any task that touches how the UI **looks, feels, moves, or is interacted with**
(pages, components, color, typography, spacing, layout, accessibility, animation,
data-viz) MUST go through the installed **`ui-ux-pro-max`** skill first — do not
hand-pick colors/fonts/styles from memory.

The skill and its sibling design skills live in `.claude/skills/`. Run the search
tool by its real path (this is a project skill, not a plugin, so
`${CLAUDE_PLUGIN_ROOT}` is not set):

```bash
python3 .claude/skills/ui-ux-pro-max/scripts/search.py "<query>" --domain <domain>
python3 .claude/skills/ui-ux-pro-max/scripts/search.py "<query>" --design-system -p "Agentic Testing Pipeline"
```

### Source of truth for this project's design

`design-system/agentic-testing-pipeline/MASTER.md` — the persisted design system.
Read it before building or restyling any UI. Page-specific overrides go in
`design-system/agentic-testing-pipeline/pages/<page>.md` and win over MASTER.md.

Established decisions (see MASTER.md for full detail):
- **Style:** Real-Time Monitoring — dark, status-driven, data-dense (density 8/10).
- **Palette:** Slate surfaces (`#0F172A`/`#1E293B`) + status colors — green `#22C55E`
  (pass/run), red `#EF4444` (fail), amber `#D29922` (warn).
- **Typography:** "Dashboard Data" — Fira Code (data/mono) + Fira Sans (labels/prose).
  (Auto-match had returned a luxury serif; corrected — keep it corrected.)

### Non-negotiables (from the skill's pre-delivery checklist)
- Contrast ≥ 4.5:1; visible keyboard focus; respect `prefers-reduced-motion`.
- SVG icons, never emoji-as-icons; `cursor:pointer` on clickables.
- Smooth 150–300ms hover/transition states; responsive at 375/768/1024/1440px.
