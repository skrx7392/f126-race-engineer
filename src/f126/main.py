"""CLI entry point: f126 serve | replay | backfill.

NOTE (subagents): this file is integration-owned — do not edit. Your module exposes the
functions referenced here; final wiring happens at integration time.
"""

from __future__ import annotations

import argparse
import sys

from f126 import config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="f126", description="F1 25 race engineer")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("serve", help="run UDP capture + web dashboard")

    p_replay = sub.add_parser("replay", help="replay a .f1raw capture through the pipeline")
    p_replay.add_argument("file")
    p_replay.add_argument("--speed", default="1", help="playback multiplier or 'max'")
    p_replay.add_argument("--loop", action="store_true")

    p_backfill = sub.add_parser("backfill", help="re-parse raw captures into the database")
    p_backfill.add_argument("paths", nargs="*", help="files/dirs; default: DATA_DIR/raw")

    p_debrief = sub.add_parser("debrief", help="write the post-session debrief for one session")
    p_debrief.add_argument("session_id", type=int, help="sessions.id, as shown by /api/sessions")
    p_debrief.add_argument(
        "--regenerate",
        action="store_true",
        help="write a new debrief even if one exists (the old one is kept)",
    )

    args = parser.parse_args(argv)
    cfg = config.load()

    if args.command == "serve":
        from f126.app import run_serve  # wired at integration

        return run_serve(cfg)
    if args.command == "replay":
        from f126.app import run_replay

        return run_replay(cfg, args.file, speed=args.speed, loop=args.loop)
    if args.command == "backfill":
        from f126.app import run_backfill

        return run_backfill(cfg, args.paths)
    if args.command == "debrief":
        from f126.app import run_debrief

        return run_debrief(cfg, args.session_id, regenerate=args.regenerate)
    return 2


if __name__ == "__main__":
    sys.exit(main())
