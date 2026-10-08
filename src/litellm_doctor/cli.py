"""litellm-doctor command line. Exit codes: 0 report printed, 2 bad input."""

from __future__ import annotations

import argparse
import math
import sys
from typing import Callable, List, Optional

from litellm_doctor import __version__
from litellm_doctor.config import ConfigError, LoadedConfig, load
from litellm_doctor.health_checks import DEFAULT_SHARED_TTL, analyze, describe
from litellm_doctor.topology import Topology


def _whole_number(minimum: int) -> Callable[[str], int]:
    def parse(text: str) -> int:
        try:
            n = int(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"expected a whole number, got {text!r}") from None
        if n < minimum:
            raise argparse.ArgumentTypeError(f"must be {minimum} or more, got {n}")
        return n
    return parse


def _usd(text: str) -> float:
    try:
        amount = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a dollar amount like 0.0003, got {text!r}") from None
    if not math.isfinite(amount) or amount < 0:
        raise argparse.ArgumentTypeError(f"must be a dollar amount of 0 or more, got {text}")
    return amount


def _topology_parser() -> argparse.ArgumentParser:
    """Flags every check that depends on scale shares. They override what a Helm values file says."""
    parent = argparse.ArgumentParser(add_help=False)
    group = parent.add_argument_group("deployment shape (read from Helm values when you pass a values file)")
    group.add_argument("--replicas", type=_whole_number(1), metavar="N",
                       help="proxy pods, or gateway pods with --split (default 1)")
    group.add_argument("--workers", type=_whole_number(1), metavar="N",
                       help="uvicorn workers per pod: --num_workers or NUM_WORKERS (default 1)")
    group.add_argument("--split", action="store_true", default=None,
                       help="you run the litellm-gateway and litellm-backend images")
    group.add_argument("--backend-replicas", type=_whole_number(0), metavar="N",
                       help="backend pods, with --split (default 1)")
    group.add_argument("--backend-workers", type=_whole_number(1), metavar="N",
                       help="uvicorn workers per backend pod, with --split (default 1)")
    group.add_argument("--collector", action="store_true", default=None,
                       help="each proxy pod runs the collector sidecar (python -m litellm.proxy.collector)")
    return parent


def _topology(args: argparse.Namespace, loaded: LoadedConfig) -> Topology:
    hints = loaded.hints

    def pick(flag, hint, default):
        return flag if flag is not None else (hint if hint is not None else default)

    split = bool(pick(args.split, hints.split, False))
    if not split and (args.backend_replicas is not None or args.backend_workers is not None):
        raise ConfigError("--backend-replicas and --backend-workers only apply with --split")
    return Topology(
        replicas=pick(args.replicas, hints.replicas, 1),
        workers=pick(args.workers, hints.workers, 1),
        split=split,
        backend_replicas=pick(args.backend_replicas, hints.backend_replicas, 1),
        backend_workers=pick(args.backend_workers, None, 1),
        collector=bool(pick(args.collector, hints.collector, False)),
    )


def _header(loaded: LoadedConfig, topology: Topology) -> List[str]:
    lines = [f"Config: {loaded.source}"]
    if loaded.included_files:
        count = len(loaded.included_files)
        lines.append(f"Merged {count} included {'file' if count == 1 else 'files'}: {', '.join(loaded.included_files)}")
    if loaded.hints.from_values_file:
        lines.append(f"Shape from the values file, with any flags you passed applied: {topology.describe()}")
    lines += loaded.hints.notes
    return lines


def _health_checks(args: argparse.Namespace) -> List[str]:
    loaded = load(args.config)
    topology = _topology(args, loaded)
    report = analyze(loaded.proxy_config, topology, shared_ttl=args.shared_ttl, db_deployments=args.db_deployments,
                     redis_from_env=args.redis_env, restarts_per_day=args.restarts_per_day)
    return _header(loaded, topology) + [""] + describe(report, args.cost_per_probe)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="litellm-doctor",
        description="Checks a LiteLLM proxy deployment for settings that cost money or break at scale. "
                    "Reads your config file. It doesn't connect to the proxy.")
    parser.add_argument("--version", action="version", version=f"litellm-doctor {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="CHECK", required=True)

    hc = sub.add_parser("health-checks", parents=[_topology_parser()],
                        help="count the provider calls background health checks make",
                        description="Counts the provider calls LiteLLM's background health checks make per day.")
    hc.add_argument("config", help="the proxy's config.yaml, or a values.yaml for the litellm-helm or litellm chart")
    hc.add_argument("--db-deployments", type=_whole_number(0), default=0, metavar="N",
                    help="deployments added through the API or UI (store_model_in_db), added to the config's count")
    hc.add_argument("--redis-env", action="store_true",
                    help="the proxy gets Redis from REDIS_HOST or REDIS_URL, which the config can't show")
    hc.add_argument("--shared-ttl", type=_whole_number(1), default=DEFAULT_SHARED_TTL, metavar="SECONDS",
                    help=f"DEFAULT_SHARED_HEALTH_CHECK_TTL, if you set that env var (default {DEFAULT_SHARED_TTL})")
    hc.add_argument("--restarts-per-day", type=_whole_number(0), default=0, metavar="N",
                    help="process starts a day (deploys, autoscaling, MAX_REQUESTS_BEFORE_RESTART). "
                         "Each start probes right away")
    hc.add_argument("--cost-per-probe", type=_usd, metavar="USD",
                    help="what 1 health check call costs, to print a cost")
    hc.set_defaults(run=_health_checks)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        lines = args.run(args)
    except ConfigError as e:
        print(f"litellm-doctor: {e}", file=sys.stderr)
        return 2
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
