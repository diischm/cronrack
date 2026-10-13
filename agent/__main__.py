"""Command line entry point: python -m agent [--once] [--config PATH]."""

import argparse
import json
import sys

from agent.config import DEFAULT_CONFIG_PATH, ConfigError, load_config
from agent.report import build_report

EXIT_OK = 0
EXIT_CONFIG_ERROR = 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m agent", description="CronRack host agent.")
    parser.add_argument(
        "--once", action="store_true", help="collect one report, print it as JSON, and exit"
    )
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG_PATH,
        metavar="PATH",
        help=f"config file (default: {DEFAULT_CONFIG_PATH})",
    )
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"cronrack-agent: config error: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    if not args.once:
        print("Connection loop arrives in Phase 7")
        return EXIT_OK

    # json.dumps escapes control characters, so a hostile container or unit
    # name can't inject terminal escape sequences into the output.
    print(json.dumps(build_report(config), indent=2))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
