"""Rule planning and an injectable, schema-validated optional model provider."""

from urllib.parse import urlsplit

from omnicrawler.pipeline import CollectionConfig


def plan(goal: str, **scope) -> CollectionConfig:
    if not goal.strip():
        raise ValueError("Goal is required")
    return CollectionConfig(goal=goal, **scope)


async def plan_with_provider(
    goal: str, provider, allowed_hosts: set[str]
) -> CollectionConfig:
    proposal = await provider(goal)
    config = CollectionConfig.model_validate(proposal)
    if (
        config.local_origins
        or not set(config.allowed_hosts) <= allowed_hosts
        or any(urlsplit(url).hostname not in allowed_hosts for url in config.urls)
    ):
        raise ValueError("Provider cannot expand authorized sources")
    config.goal = goal
    return config
