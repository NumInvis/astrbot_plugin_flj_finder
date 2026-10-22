"""Search one image with real engines, without sending bot messages."""

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from search import SearchService  # noqa: E402


async def run(args):
    config = (
        json.loads(args.config.read_text(encoding="utf-8-sig")) if args.config else {}
    )
    if args.engines is not None:
        config["engines"] = args.engines
    if args.timeout is not None:
        config["search_timeout"] = args.timeout
    service = SearchService(config)
    try:
        report = await service.search(args.image.read_bytes())
        print(json.dumps(asdict(report), ensure_ascii=False, indent=2))
        return 0 if report.results else 1
    finally:
        await service.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path)
    parser.add_argument(
        "--config", type=Path, help="Read plugin config without printing API keys"
    )
    parser.add_argument("--engines", nargs="+")
    parser.add_argument("--timeout", type=int)
    sys.exit(asyncio.run(run(parser.parse_args())))
