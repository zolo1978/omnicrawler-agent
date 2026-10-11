"""Conservative field corroboration and minimal personal-data filtering."""

import re
from collections import defaultdict


def sanitize(text: str) -> str:
    return re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[redacted-email]", text)


def assess(records) -> list[dict]:
    groups = defaultdict(list)
    for record in records:
        for field, value in record.fields.items():
            groups[(record.entity_id, field)].append(
                {
                    "value": value,
                    "evidence": record.evidence[field].model_dump(mode="json"),
                }
            )
    output = []
    for (entity, field), observations in sorted(groups.items()):
        values = {str(observation["value"]) for observation in observations}
        output.append(
            {
                "entity_id": entity,
                "field": field,
                "kind": "fact",
                "status": "disputed"
                if len(values) > 1
                else "agreement"
                if len(observations) > 1
                else "single_source",
                "independence_verified": False,
                "observations": observations,
            }
        )
    return output
