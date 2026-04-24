"""Reddit JSON API adapter."""

from __future__ import annotations

from typing import Any

import httpx

from digest.irritator.sources import Signal, _register

_TIMEOUT = 10.0
_MAX_RESULTS = 10
_USER_AGENT = "digest-bot/2.0 (counter-signal search)"


@_register("reddit")
async def search_reddit(
    query: str,
    config: Any,
    client: httpx.AsyncClient,
) -> list[Signal]:
    """Search Reddit via the public JSON API across configured subreddits."""
    subreddits = getattr(config.irritator, "reddit_subreddits", ["programming"])
    sub_str = "+".join(subreddits)
    url = f"https://www.reddit.com/r/{sub_str}/search.json"

    resp = await client.get(
        url,
        params={"q": query, "sort": "relevance", "limit": _MAX_RESULTS, "restrict_sr": "on"},
        headers={"User-Agent": _USER_AGENT},
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()

    signals: list[Signal] = []
    for child in data.get("data", {}).get("children", []):
        post = child.get("data", {})
        signals.append(
            Signal(
                url=f"https://reddit.com{post.get('permalink', '')}",
                title=post.get("title", ""),
                snippet=post.get("selftext", "")[:500],
                source_name="reddit",
                published=str(post.get("created_utc", "")),
                score=float(post.get("score", 0)),
            )
        )
    return signals
