"""Lobsters configuration remains recognized without unsupported HTTP search."""

import httpx
import pytest

from digest.irritator.sources import SourceUnavailableError
from digest.irritator.sources.lobsters import search_lobsters


@pytest.mark.asyncio
async def test_unsupported_lobsters_search_makes_no_request() -> None:
    def unexpected(request: httpx.Request) -> httpx.Response:
        raise AssertionError("Unsupported search must not issue HTTP")

    async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected)) as client:
        with pytest.raises(SourceUnavailableError, match="no supported JSON search contract"):
            await search_lobsters("example", None, client)
