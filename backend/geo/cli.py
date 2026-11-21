"""Operator commands: `python -m geo.cli <command>`."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from geo.catalog import DATA_DIR


def main() -> None:
    ap = argparse.ArgumentParser(prog="geo")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate", help="apply SQL migrations")
    sub.add_parser("seed", help="load prompts/brands/platforms config from data/*.csv")
    h = sub.add_parser("load-history", help="load a Bright Data .jsonl(.gz) export into raw.answers")
    h.add_argument("path", type=Path, nargs="?", default=DATA_DIR / "history.jsonl.gz")
    sub.add_parser("pipeline", help="parse new raw answers and refresh daily metrics")
    sub.add_parser("insights", help="detect changes and store an insights batch")
    r = sub.add_parser("report", help="build the report for a brand and store it")
    r.add_argument("--brand", default=None)
    r.add_argument("--out", type=Path, default=None)
    s = sub.add_parser("schedule", help="create or update the Temporal collection schedule")
    s.add_argument("--pause", action="store_true")
    args = ap.parse_args()

    if args.cmd == "migrate":
        from geo.db.migrate import migrate
        print("applied:", migrate() or "nothing new")
    elif args.cmd == "seed":
        from geo.etl.pipeline import seed_catalog
        seed_catalog()
        print("seeded")
    elif args.cmd == "load-history":
        from geo.etl.pipeline import load_history
        print(load_history(args.path))
    elif args.cmd == "pipeline":
        from geo.etl.pipeline import run_pipeline
        print(json.dumps(run_pipeline(), default=str, indent=2))
    elif args.cmd == "insights":
        from geo.insights.detect import run_insights
        print(json.dumps(run_insights(), default=str, indent=2))
    elif args.cmd == "report":
        from geo.report.build import build_and_store
        report_id, html = build_and_store(args.brand)
        if args.out:
            args.out.write_text(html)
        print("report", report_id)
    elif args.cmd == "schedule":
        import asyncio

        from geo.workflows.schedule import ensure_schedule
        print(asyncio.run(ensure_schedule(paused=args.pause)))


if __name__ == "__main__":
    main()
