"""Bounded feedback collection and acknowledgement for a managed durable runtime.

Run collect, commit/push feedback.json, then ack with the committed blob's SHA256.
The regular digest must use --feedback-precollected even if this optional stage fails.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from digest.adapters.storage.feedback import FEEDBACK_FILE, decode_feedback, load_feedback
from digest.application.feedback import acknowledge_feedback, collect_feedback


def _log_local_collection(timestamp: str, *, now: datetime, previous: bool) -> None:
    """Report retained local evidence without exposing unusable timestamp strings."""
    label = "Previous retained successful local collection" if previous else "Persisted successful local collection"
    reason = "missing"
    if timestamp:
        try:
            retained = datetime.fromisoformat(timestamp)
            if retained.tzinfo is None:
                reason = "naive"
            else:
                retained = retained.astimezone(timezone.utc)
                age = (now - retained).total_seconds()
                if age >= 0:
                    logging.info("%s: at=%s age_seconds=%d", label, retained.isoformat(), int(age))
                    return
                reason = "future"
        except (ValueError, OverflowError):
            reason = "malformed"
    logging.info("%s: at=unknown age_seconds=unknown reason=%s", label, reason)


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("collect", "ack"))
    parser.add_argument("--cache-dir", default=".cache")
    parser.add_argument("--expected-sha256", help="SHA256 of feedback.json from the successfully pushed Git commit")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    owner = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not owner:
        logging.error("Existing TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required.")
        return 1
    try:
        if args.command == "collect":
            store = load_feedback(args.cache_dir, strict=True)
            _log_local_collection(store.last_successful_poll_at, now=datetime.now(timezone.utc), previous=True)
            store = await collect_feedback(token, store, cache_dir=args.cache_dir, acknowledge=False)
            stats = store.last_poll_counts
            path = Path(args.cache_dir) / FEEDBACK_FILE
            content = path.read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            persisted = decode_feedback(content, strict=True)
            output = os.environ.get("GITHUB_OUTPUT")
            if output:
                with Path(output).open("a", encoding="utf-8") as handle:
                    handle.write(f"feedback_sha256={digest}\n")
            _log_local_collection(persisted.last_successful_poll_at, now=datetime.now(timezone.utc), previous=False)
        else:
            if not args.expected_sha256 or not re.fullmatch(r"[a-f0-9]{64}", args.expected_sha256):
                raise ValueError("Acknowledgement requires the exact committed feedback SHA256.")
            stats = await acknowledge_feedback(token, args.cache_dir, args.expected_sha256)
        print(json.dumps({"stage": args.command, "counts": stats}, sort_keys=True))
        return 0
    except Exception as exc:
        # Never print raw Telegram responses, callback IDs, request URLs or user IDs.
        logging.error("Feedback %s incomplete (%s); do not advance an uncommitted offset.",
                      args.command, type(exc).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
