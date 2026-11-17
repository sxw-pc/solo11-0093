"""Unified command line interface for the lake commit verification bench."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from lakecommit import config, linecount, runner
from lakecommit.errors import LakeCommitError
from lakecommit.fingerprint import pretty_json


def _summary(report: dict, workdir: str, *, golden: bool | None = None) -> dict:
    value = {
        "scenario_id": report["scenario"]["id"],
        "workdir": str(Path(workdir)),
        "attempts": report["summary"]["attempts"],
        "accepted": report["summary"]["accepted"],
        "rejected": report["summary"]["rejected"],
        "replayed": report["summary"]["replayed"],
        "final_snapshot_id": report["final_snapshot"]["snapshot_id"],
        "final_file_count": report["final_snapshot"]["file_count"],
        "report": str(Path(workdir) / config.REPORT_JSON),
        "run_fingerprint": report["run_fingerprint"],
    }
    if golden is not None:
        value["golden_match"] = golden
    return value


def _print_json(value: object) -> None:
    sys.stdout.write(pretty_json(value))
    sys.stdout.flush()


def command_demo(args: argparse.Namespace) -> int:
    report = runner.run_scenario(config.DEMO_SCENARIO, args.workdir)
    golden = runner.verify_golden(args.workdir)
    _print_json(_summary(report, args.workdir, golden=bool(golden["match"])))
    return 0


def command_run(args: argparse.Namespace) -> int:
    report = runner.run_scenario(args.scenario, args.workdir)
    _print_json(_summary(report, args.workdir))
    return 0


def command_explain(args: argparse.Namespace) -> int:
    _print_json(runner.explain_attempt(args.workdir, args.attempt))
    return 0


def command_verify_golden(args: argparse.Namespace) -> int:
    _print_json(runner.verify_golden(args.workdir))
    return 0


def command_verify(args: argparse.Namespace) -> int:
    from lakecommit.store import SnapshotStore

    verification = SnapshotStore(args.workdir).verify()
    _print_json(verification)
    return 0 if verification["ok"] else 3


def command_serve(args: argparse.Namespace) -> int:
    from lakecommit import server

    server.serve(args.workdir, host=args.host, port=args.port)
    return 0


def command_linecount(args: argparse.Namespace) -> int:
    report = linecount.collect(args.root)
    if args.json:
        _print_json(report)
    else:
        sys.stdout.write(linecount.format_report(report) + "\n")
        sys.stdout.flush()
    return 0 if report["budget_ok"] else 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lakecommit",
        description="Offline, deterministic file-level lake table commit verification bench.",
    )
    parser.add_argument("--version", action="version", version=f"lakecommit {config.SCHEMA_VERSION}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    demo = subparsers.add_parser("demo", help="run the built-in mixed-workload scenario")
    demo.add_argument("--workdir", default=str(config.DEFAULT_WORKDIR))
    demo.set_defaults(handler=command_demo)

    run = subparsers.add_parser("run", help="run one YAML scenario")
    run.add_argument("--scenario", required=True, help="path to a scenario YAML file")
    run.add_argument("--workdir", default=".lakecommit-run")
    run.set_defaults(handler=command_run)

    explain = subparsers.add_parser("explain", help="explain one audited attempt")
    explain.add_argument("--workdir", default=str(config.DEFAULT_WORKDIR))
    explain.add_argument("--attempt", required=True)
    explain.set_defaults(handler=command_explain)

    golden = subparsers.add_parser("verify-golden", help="compare report.json with its golden result")
    golden.add_argument("--workdir", default=str(config.DEFAULT_WORKDIR))
    golden.set_defaults(handler=command_verify_golden)

    verify = subparsers.add_parser("verify", help="verify the immutable snapshot chain")
    verify.add_argument("--workdir", default=str(config.DEFAULT_WORKDIR))
    verify.set_defaults(handler=command_verify)

    serve = subparsers.add_parser("serve", help="start the read-only loopback console")
    serve.add_argument("--host", default=config.DEFAULT_HOST)
    serve.add_argument("--port", type=int, default=config.DEFAULT_PORT)
    serve.add_argument("--workdir", default=str(config.DEFAULT_WORKDIR))
    serve.set_defaults(handler=command_serve)

    counting = subparsers.add_parser("linecount", help="check the Python code-size budget")
    counting.add_argument("--root", default=".")
    counting.add_argument("--json", action="store_true")
    counting.set_defaults(handler=command_linecount)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except LakeCommitError as exc:
        sys.stderr.write(pretty_json(exc.as_dict()))
        return exc.exit_code
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # keep the CLI contract stable for unexpected failures
        sys.stderr.write(
            pretty_json(
                {
                    "error": {
                        "code": "INTERNAL_ERROR",
                        "message": "unexpected failure",
                        "details": {"type": exc.__class__.__name__},
                    }
                }
            )
        )
        return 1
