## Connect a backend
The extension talks to the **AI Testing Marketplace** platform over HTTP.

**Option A — run it locally**
```bash
git clone https://github.com/aiqalearning/ai-testing-marketplace
cd agentic-testing-pipeline && python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt && cp .env.example .env   # add your LLM key
PORT=8090 python -m web.server
```
Then set **AI Testing Marketplace › Platform Path** to that folder and use *Start local backend*.

**Option B — hosted**
Set **AI Testing Marketplace › Backend URL** to your team's instance (e.g. `https://testing.yourcompany.com`).
