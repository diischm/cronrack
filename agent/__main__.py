"""Command line entry point: python -m agent [--once] [--config PATH].

Without --once the agent connects to the server and stays connected until it
gets SIGTERM (systemctl stop) or SIGINT (Ctrl+C); then it closes the socket
cleanly and exits 0. Logs go to stderr, where journald collects them.
"""

import argparse
import asyncio
import contextlib
import json
import logging
import signal
import sys

from agent.config import DEFAULT_CONFIG_PATH, TOKEN_ENV_VAR, ConfigError, load_config
from agent.connection import run_forever
from agent.report import build_report

EXIT_OK = 0
EXIT_CONFIG_ERROR = 2

logger = logging.getLogger("agent")


def configure_logging() -> None:
    # No timestamps: journald adds its own.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


async def run_until_signal(config) -> None:
    """run_forever() until SIGTERM or SIGINT, then close the connection and return."""
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    runner = asyncio.create_task(run_forever(config))
    waiter = asyncio.create_task(stop.wait())
    try:
        await asyncio.wait({runner, waiter}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)
        waiter.cancel()
        # Cancelling leaves `async with connect(...)`, which sends a normal close (1000).
        runner.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await runner
    logger.info("Stopped")


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

    if args.once:
        # json.dumps escapes control characters, so a hostile container or unit
        # name can't inject terminal escape sequences into the output.
        print(json.dumps(build_report(config), indent=2))
        return EXIT_OK

    # No token, no start (and no server, nowhere to go).
    if not config.server_url:
        print(f"cronrack-agent: config error: set server_url in {args.config}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    if not config.token:
        print(
            f"cronrack-agent: config error: no token; set token in {args.config} "
            f"or the {TOKEN_ENV_VAR} environment variable",
            file=sys.stderr,
        )
        return EXIT_CONFIG_ERROR

    configure_logging()
    logger.info("CronRack agent starting; server %s", config.server_url)
    asyncio.run(run_until_signal(config))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
