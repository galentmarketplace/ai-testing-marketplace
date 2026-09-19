"""CLI entry point.

Usage:
  MOCK_LLM=1 python -m src.main                 # demo with canned LLM responses
  python -m src.main --story sample_story.json  # real Claude calls (needs .env)
"""
import argparse
import json
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from .graph import build_graph  # noqa: E402

DEFAULT_STORY = {
    "id": "DEMO-1",
    "title": "Invoice status transitions",
    "description": (
        "As an accountant, I want invoice statuses to move Draft -> Sent -> Paid "
        "so that I can track billing. Creating an invoice sets status Draft. "
        "Recording a full payment sets status Paid. A customer must be selected "
        "before an invoice can be saved."
    ),
}


def main():
    parser = argparse.ArgumentParser(description="Agentic testing pipeline")
    parser.add_argument("--story", help="Path to a story JSON file", default=None)
    args = parser.parse_args()

    story = DEFAULT_STORY
    if args.story:
        story = json.loads(Path(args.story).read_text())

    print(f"\n=== Pipeline start: {story['id']} — {story['title']} ===\n")
    graph = build_graph()
    final = graph.invoke({"story": story, "status": "running", "attempts": {}},
                         config={"recursion_limit": 50})

    print(f"\n=== Pipeline finished: status={final.get('status')} ===")
    print(f"Gates: {[(d['gate'], d['verdict']) for d in final.get('gate_decisions', [])]}")
    if final.get("pr"):
        print(f"\nPR draft:\n  {final['pr']['title']}\n  {final['pr']['url']}")

    out = Path("pipeline_result.json")
    out.write_text(json.dumps(final, indent=2, default=str))
    print(f"\nFull state written to {out}")


if __name__ == "__main__":
    main()
