"""Conservative field corroboration and minimal personal-data filtering."""

import re
from collections import defaultdict
from urllib.parse import urlsplit


def sanitize(text: str) -> str:
    return re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[redacted-email]", text)


def assess(records) -> list[dict]:
    """Group field observations per entity and state how well they corroborate.

    Independence is *host* diversity, not observation count: two pages on one host
    share a publisher and its mistakes, so their agreement proves nothing beyond
    self-consistency. ``independent_sources`` reports how many distinct hostnames
    actually backed the field, and ``independence_verified`` is true only when at
    least two of them did.
    """
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
        hosts = {
            urlsplit(str(observation["evidence"]["url"])).hostname or ""
            for observation in observations
        }
        hosts.discard("")
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
                "independence_verified": len(hosts) >= 2,
                "independent_sources": len(hosts),
                "observations": observations,
            }
        )
    return output
