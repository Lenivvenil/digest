"""Parse the existing command-line interface without starting application work."""
from __future__ import annotations

import argparse


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Daily News Digest — Radar + Irritator v2"
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to config.yaml (default: config.yaml)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run pipeline without sending to Telegram or writing files",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate config, check env vars, and probe feed URLs, then exit",
    )
    parser.add_argument(
        "--radar-only",
        action="store_true",
        help="Run only the radar pipeline (skip irritator)",
    )
    parser.add_argument(
        "--discover",
        action="store_true",
        help="Use LLM to suggest new RSS sources for underrepresented categories, then exit",
    )
    parser.add_argument("--discovery-phase", choices=("all", "prepare", "send"), default="all",
                        help="Discovery preparation and externally persisted send phases (default: local-only all)")
    parser.add_argument("--discovery-pending-sha", help="SHA256 of remotely persisted pending proposals")
    parser.add_argument("--discovery-delivery-sha", help="SHA256 of remotely persisted discovery delivery metadata")
    parser.add_argument(
        "--feedback-precollected", action="store_true",
        help="Managed runtime owns feedback collection/persistence; do not poll again in this process",
    )
    parser.add_argument("--reserve-issue", action="store_true",
                        help="Reserve one compact issue locally; managed runtime must commit/push before publication")
    parser.add_argument("--issue-reservation-sha",
                        help="SHA256 of the externally persisted compact issue reservation")
    parser.add_argument("--prepare-edition", action="store_true",
                        help="Prepare and freeze an edition without claiming or sending it")
    parser.add_argument("--edition-date", help="Intended UTC publication date YYYY-MM-DD (prepare only)")
    parser.add_argument("--edition-phase", choices=("inspect", "claim", "send"),
                        help="Inspect or deliver an already prepared immutable edition")
    parser.add_argument("--ready-sha", help="SHA256 of the remotely persisted ready edition")
    parser.add_argument("--claim-sha", help="SHA256 of the remotely persisted delivery claim")
    return parser.parse_args(argv)
