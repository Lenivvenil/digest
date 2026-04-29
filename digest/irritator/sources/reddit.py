"""Reddit JSON API adapter — OAuth2 client_credentials."""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from digest.irritator.sources import Signal, _register

logger = logging.getLogger(__name__)

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
        logger.info("Reddit credentials not configured — skipping Reddit source")
        return []

    username = os.environ.get("REDDIT_USERNAME", "digest-bot")
    user_agent = f"script:digest-bot:2.0 (by /u/{username})"

    token_resp = await client.post(
        _TOKEN_URL,
        auth=(client_id, client_secret),
        data={"grant_type": "client_credentials"},
        headers={"User-Agent": user_agent},
        timeout=_TIMEOUT,
    )
    if token_resp.status_code >= 400:
        logger.warning(
            "Reddit token fetch failed (%d) — skipping Reddit source",
            token_resp.status_code,
        )
        return []
    try:
        access_token: str = token_resp.json().get("access_token", "")
    except Exception:
        logger.warning("Reddit token response is not valid JSON — skipping Reddit source")
        return []
    if not access_token:
        logger.warning("Reddit token response missing access_token — skipping Reddit source")
        return []

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
