"""Explicit data-only prefill setup; does not run a model, fetch a feed or send."""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from digest.application.discovery import start_session
from digest.application.native_discovery import (
    NativeDiscoveryInput,
    NativeDiscoveryUnavailable,
    bind_prefill,
    export_request,
    reserve_prefill,
)
from digest.config import load_config


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="phase", required=True)
    export = commands.add_parser("export", help="Retain immutable request; controller owns one durable worker launch")
    export.add_argument("--config", required=True)
    export.add_argument("--slot", required=True, help="Scheduled UTC start, e.g. 2026-10-25T08:00:00+00:00")
    bind = commands.add_parser("bind", help="Bind exact response to independently observed native worker receipt")
    bind.add_argument("--request", required=True, type=Path)
    bind.add_argument("--response", required=True, type=Path)
    bind.add_argument("--execution-ref", required=True)
    reserve = commands.add_parser("reserve", help="Same weekly run: reserve, then externally persist before prepare")
    reserve.add_argument("--config", required=True)
    reserve.add_argument("--prefill", required=True, type=Path)
    reserve.add_argument("--prefill-sha", required=True)
    for command in (export, bind, reserve):
        command.add_argument("--controller", required=True,
                             help="Actual trusted native controller, not model assertion")
        command.add_argument("--cache", type=Path, default=Path(".cache"))
    args = parser.parse_args(argv)
    if args.phase == "export":
        reference = export_request(load_config(args.config), cache=args.cache,
                                   slot=datetime.fromisoformat(args.slot), controller_ref=args.controller)
    elif args.phase == "bind":
        reference = bind_prefill(args.request, args.response, execution_ref=args.execution_ref,
                                 controller_ref=args.controller, cache=args.cache)
    else:
        from dataclasses import replace

        session = replace(start_session(args.config, "prepare"), cache_dir=str(args.cache))
        try:
            digest = reserve_prefill(session, NativeDiscoveryInput(args.prefill, args.prefill_sha, args.controller))
        except NativeDiscoveryUnavailable as exc:
            print(json.dumps({"status": "unavailable", "reservation_sha256": "", "reason": str(exc)}, sort_keys=True))
            return
        print(json.dumps({"status": "reserved", "reservation_sha256": digest}, sort_keys=True))
        return
    print(json.dumps({"path": reference.path, "sha256": reference.sha256}, sort_keys=True))


if __name__ == "__main__":
    main()
