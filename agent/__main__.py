"""Command line entry point: python -m agent [--once | --check-k8s] [--config PATH].

Without --once the agent connects to the server and stays connected until it
gets SIGTERM (systemctl stop) or SIGINT (Ctrl+C); then it closes the socket
cleanly and exits 0. Logs go to stderr, where journald collects them.

--check-k8s asks the Kubernetes API what the agent's service account may read and
prints one line per API (never the token or any cluster data). Exit 0 = all fine,
3 = something is denied or unreachable, 2 = the config isn't set up for it.
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
from agent.kube_api import KubeClient, permission_report
from agent.report import build_report

EXIT_OK = 0
EXIT_CONFIG_ERROR = 2
EXIT_K8S_PROBLEM = 3

# How --check-k8s words each outcome.
K8S_RESULTS = {
    "ok": "ok",
    "denied": "denied (403)",
    "missing": "not installed (404)",
    "unauthorized": "unauthorized (401)",
    "error": "error (unexpected answer)",
}
K8S_ERRORS = {
    "refused": "connection refused or unreachable",
    "tls": "TLS certificate not trusted (check ca_file and api_server)",
    "timeout": "timed out",
    "unauthorized": "the cluster doesn't accept the token (401)",
    "bad_response": "unexpected answer",
}

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


def check_k8s(config, path: str) -> int:
    """Print what the service account may read. Exit 0 if all is well, 3 if not."""
    kube = config.kubernetes
    if not kube.cluster_view:
        print(
            f"cronrack-agent: config error: set cluster_view = true (with token_file and "
            f"ca_file) in the [kubernetes] table of {path}",
            file=sys.stderr,
        )
        return EXIT_CONFIG_ERROR
    result = permission_report(KubeClient(kube.api_server, kube.token_file, kube.ca_file))

    state = "reachable" if result["reachable"] else "NOT reachable"
    print(f"API server:      {kube.api_server} ({state})")
    if result["error"]:
        print(f"Problem:         {K8S_ERRORS.get(result['error'], result['error'])}")
    if result["cert_days"] is not None:
        print(f"API certificate: {result['cert_days']} days left")
    if result["sa"]:
        print(f"Service account: {result['sa']}")
    if result["token_days"] is not None:
        print(f"Token:           {result['token_days']} days left")
    if result["rows"]:
        print()
        print(f"{'resource':<44} {'verb':<5} result")
    problem = bool(result["error"]) or not result["reachable"]
    for row in result["rows"]:
        name = (
            row["resource"]
            if row["resource"].startswith("/")
            else (f"{row['group'] or 'core'}/{row['resource']}")
        )
        print(f"{name:<44} {row['verb']:<5} {K8S_RESULTS.get(row['result'], row['result'])}")
        problem = problem or row["result"] in ("denied", "unauthorized", "error")
    return EXIT_K8S_PROBLEM if problem else EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m agent", description="CronRack host agent.")
    parser.add_argument(
        "--once", action="store_true", help="collect one report, print it as JSON, and exit"
    )
    parser.add_argument(
        "--check-k8s",
        action="store_true",
        help="check what the Kubernetes service account may read, then exit "
        "(0 = fine, 3 = something denied or unreachable)",
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

    if args.check_k8s:
        return check_k8s(config, args.config)

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
