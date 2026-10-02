"""Reddit JSON API adapter — OAuth2 client_credentials."""

from __future__ import annotations

import os
from typing import Any

import httpx

from digest.irritator.sources import Signal, SourceUnavailableError, _register, validate_search_response

_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
_SEARCH_BASE = "https://oauth.reddit.com"
_TIMEOUT = 10.0
_MAX_RESULTS = 10


@_register("reddit")
async def search_reddit(
    query: str,
    config: Any,
    client: httpx.AsyncClient,
) -> list[Signal]:
    """Search Reddit via OAuth2 client_credentials across configured subreddits."""
    client_id = os.environ.get("REDDIT_CLIENT_ID", "")
    client_secret = os.environ.get("REDDIT_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        raise SourceUnavailableError("Reddit credentials are not configured.")

    username = os.environ.get("REDDIT_USERNAME", "digest-bot")
    user_agent = f"script:digest-bot:2.0 (by /u/{username})"

    token_resp = await client.post(
        _TOKEN_URL,
        auth=(client_id, client_secret),
        data={"grant_type": "client_credentials"},
        headers={"User-Agent": user_agent},
        timeout=_TIMEOUT,
    )
    token_resp.raise_for_status()
    try:
        token_data = token_resp.json()
    except ValueError:
        raise ValueError("Invalid Reddit token response.") from None
    access_token = token_data.get("access_token") if isinstance(token_data, dict) else None
    if not isinstance(access_token, str) or not access_token.strip():
        raise ValueError("Invalid Reddit token response.")

    subreddits = getattr(config.irritator, "reddit_subreddits", ["programming"])
    sub_str = "+".join(subreddits)
    url = f"{_SEARCH_BASE}/r/{sub_str}/search"

    resp = await client.get(
        url,
        params={"q": query, "sort": "relevance", "limit": _MAX_RESULTS, "restrict_sr": "on"},
        headers={
            "Authorization": f"bearer {access_token}",
            "User-Agent": user_agent,
        },
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    data = validate_search_response(resp, "reddit")

    signals: list[Signal] = []
    for child in data["data"]["children"]:
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
