"""Rule planning for explicitly configured collections.

A model is never asked to author a whole collection config: the one-shot
"goal -> URLs + CSS" plan was unreliable and unverifiable, so planning is
deterministic and the model is confined to what it can be held to — proposing
selectors against a page that was already fetched (``session.preview`` + the
two-step provider in ``provider.py``) and writing evidence-bound synthesis
(``synthesis.py``).
"""

from omnicrawler.pipeline import CollectionConfig


def plan(goal: str, **scope) -> CollectionConfig:
    if not goal.strip():
        raise ValueError("Goal is required")
    return CollectionConfig(goal=goal, **scope)
