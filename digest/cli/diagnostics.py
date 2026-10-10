"""Explicit command-line diagnostics, including bounded feed probes."""
from __future__ import annotations

import asyncio
import os
from typing import Any


async def check_config(config_path: str) -> int:
    """Validate config, check env vars, and probe all enabled feed URLs."""
    import feedparser
    import httpx

    from digest.adapters.http.public_fetch import UnsafePublicURL, fetch_public
    from digest.config import load_config

    ok = True

    print("Checking config...")
    try:
        config = load_config(config_path)
        print(f"  [OK] Config loaded: {len(config.enabled_sources)} enabled sources")
    except Exception as exc:
        print(f"  [FAIL] Config load error: {exc}")
        return 1

    print("\nChecking environment variables...")
    _provider_env_vars: dict[str, str] = {
        "anthropic": "ANTHROPIC_API_KEY",
        "gemini": "GEMINI_API_KEY",
        "groq": "GROQ_API_KEY",
        "mistral": "MISTRAL_API_KEY",
        "deepseek": "DEEPSEEK_API_KEY",
    }
    all_provider_names: set[str] = {pc.name for pc in config.llm.providers}
    for route in getattr(config.llm, "routing", []):
        all_provider_names.add(route.provider)

    for provider_name in sorted(all_provider_names):
        var = _provider_env_vars.get(provider_name, "")
        if not var:
            continue
        val = os.environ.get(var, "")
        if val:
            print(f"  [OK] {var} is set ({provider_name})")
        else:
            print(f"  [WARN] {var} is not set (required for {provider_name})")
    if config.telegram.enabled:
        for var in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
            val = os.environ.get(var, "")
            if val:
                print(f"  [OK] {var} is set")
            else:
                print(f"  [WARN] {var} is not set (required for enabled Telegram delivery)")
    else:
        print("  [SKIP] Telegram delivery is disabled; delivery credentials are not required.")

    print("\nChecking for duplicates...")
    seen_names: dict[str, int] = {}
    seen_urls: dict[str, str] = {}
    for source in config.enabled_sources:
        seen_names[source.name] = seen_names.get(source.name, 0) + 1
        if source.url in seen_urls:
            print(
                f"  [WARN] Duplicate URL: {source.url!r} used by "
                f"{seen_urls[source.url]!r} and {source.name!r}"
            )
        else:
            seen_urls[source.url] = source.name
    for name, count in seen_names.items():
        if count > 1:
            print(f"  [WARN] Duplicate source name: {name!r} appears {count} times")
    if all(c == 1 for c in seen_names.values()) and len(seen_urls) == len(config.enabled_sources):
        print("  [OK] No duplicate names or URLs")

    print(f"\nProbing {len(config.enabled_sources)} feed URLs...")

    async def _probe(source: Any) -> tuple[str, str, str]:
        try:
            resp = await fetch_public(source.url, timeout=15.0, max_bytes=2 * 1024 * 1024, max_redirects=3)
            resp.raise_for_status()
            feed = feedparser.parse(resp.content.decode(resp.encoding, errors="replace"))
            if feed.bozo and not feed.entries:
                return source.name, "WARN", f"feedparser error: {feed.bozo_exception}"
            entry_count = len(feed.entries)
            return source.name, "OK", f"{entry_count} entries"
        except UnsafePublicURL:
            return source.name, "BLOCKED", "unsafe URL (private/local/non-http)"
        except (TimeoutError, httpx.TimeoutException):
            return source.name, "FAIL", "timeout"
        except Exception as exc:
            return source.name, "FAIL", str(exc)

    tasks = [asyncio.create_task(_probe(s)) for s in config.enabled_sources]
    results = await asyncio.gather(*tasks)

    for name, status, detail in sorted(results):
        print(f"  [{status:6}] {name}: {detail}")
        if status in ("FAIL", "BLOCKED"):
            ok = False

    fail_count = sum(1 for _, s, _ in results if s in ("FAIL", "BLOCKED"))
    warn_count = sum(1 for _, s, _ in results if s == "WARN")
    ok_count_feeds = sum(1 for _, s, _ in results if s == "OK")
    print(
        f"\nFeed results: {ok_count_feeds} OK, {warn_count} WARN, {fail_count} FAIL"
        f" out of {len(config.enabled_sources)} feeds"
    )
    if ok:
        print("Feed probes completed without failures. Review any warnings above.")
    else:
        print("Some feed checks FAILED — fix the issues above before running the digest.")
    print("Model and Telegram access were not tested.")

    return 0 if ok else 1
