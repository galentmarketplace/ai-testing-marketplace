"""AC Agent — user story in, Gherkin acceptance criteria out. (Design doc step 2)"""
from ..llm import call_llm_json
from ..state import AcceptanceCriteria, PipelineState

SYSTEM = """You are a senior QA analyst. Given a user story, write acceptance criteria.

Rules:
- Gherkin format (Given/When/Then), each criterion independently testable
- Cover the happy path, at least one negative/validation case, and edge cases
- Tag each criterion with the functional area it touches (e.g. "invoice", "payment")
- priority: "must" | "should" | "could"

Respond with ONLY a JSON object:
{"story_id": "...", "criteria": [{"id": "AC-1", "gherkin": "...", "priority": "must", "tags": ["..."]}]}"""


def generate_ac(state: PipelineState) -> dict:
    story = state["story"]
    source = story.get("ac_input")  # a Jira link or pasted acceptance criteria, if the user gave one
    if source:
        user = f"Parse acceptance criteria from this source (a Jira link or criteria text):\n{source}"
    else:
        user = (f"Story ID: {story['id']}\nTitle: {story.get('title','')}\n\n"
                f"Description:\n{story.get('description','')}\n\n"
                "No explicit criteria or Jira link was given — infer the criteria from the repo/story.")
    raw = call_llm_json("ac_agent", SYSTEM, user)
    ac = AcceptanceCriteria.model_validate(raw)  # validate the contract
    print(f"  [AC Agent] generated {len(ac.criteria)} criteria for {ac.story_id}")
    return {"acceptance_criteria": ac.model_dump()}
