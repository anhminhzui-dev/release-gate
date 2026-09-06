"""Command line. `gate` decides, `explain` shows what every threshold is doing.

Exit codes: 0 GO, 2 HOLD, 1 bad usage or crash. A crash still prints a HOLD line before it
exits, so a broken run can never be mistaken for a pass by whatever reads this output next.
"""
from __future__ import annotations

import argparse
import sys

from . import gate as G

EXIT_GO = 0
EXIT_USAGE = 1
EXIT_HOLD = 2


def parse_set(pairs) -> dict:
    out: dict = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise G.GateError(f"--set wants KEY=VALUE, got: {pair}")
        key, value = pair.split("=", 1)
        try:
            float(value)
        except ValueError:
            raise G.GateError(f"--set wants a number, got: {pair}") from None
        out[key.strip()] = value.strip()
    return out


def _inputs(args, policy):
    if not args.baseline or not args.candidate:
        raise G.GateError("this verb needs both --baseline and --candidate")
    base_rows, base_refusals, base_sha = G.load_rows(args.baseline, policy, args.allow_nonsynthetic)
    cand_rows, cand_refusals, cand_sha = G.load_rows(args.candidate, policy, args.allow_nonsynthetic)
    refusals = base_refusals + cand_refusals
    stats = G.compute_stats(cand_rows, base_rows, policy)
    shas = {
        "policy_sha256": policy["sha256"],
        "baseline_sha256": base_sha,
        "candidate_sha256": cand_sha,
    }
    return stats, refusals, shas


def do_gate(args) -> tuple:
    policy = G.load_policy(args.policy)
    table, declared = G.thresholds(policy, parse_set(args.set))
    stats, refusals, shas = _inputs(args, policy)
    admission = [f"{code}:line{line}" for line, code in refusals]
    reasons = admission + G.evaluate(stats, table)
    lines = G.gate_lines(policy, stats, table, declared, reasons, refusals)
    if args.out:
        summary, trace = G.receipt(stats, table, declared, reasons, refusals, shas)
        G.write_receipt(args.out, summary, trace)
    return lines, (EXIT_HOLD if reasons else EXIT_GO)


def do_explain(args) -> tuple:
    policy = G.load_policy(args.policy)
    table, _declared = G.thresholds(policy, parse_set(args.set))
    stats = None
    if args.baseline and args.candidate:
        stats, refusals, _shas = _inputs(args, policy)
        if refusals:
            raise G.GateError(f"{len(refusals)} row(s) were refused at admission; explain needs a clean set")
    return G.explain_lines(policy, table, stats), EXIT_GO


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="release_gate.cli",
        description="Turn rubric scores into a release verdict whose every threshold is defended.",
    )
    sub = parser.add_subparsers(dest="verb", required=True)
    for verb, helptext in (("gate", "decide GO or HOLD"), ("explain", "show every threshold")):
        part = sub.add_parser(verb, help=helptext)
        part.add_argument("--policy", required=True, help="gate policy JSON")
        part.add_argument("--baseline", default=None, help="baseline results JSONL")
        part.add_argument("--candidate", default=None, help="candidate results JSONL")
        part.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                          help="override one threshold, e.g. floor.SAFETY=3.9")
        part.add_argument("--allow-nonsynthetic", action="store_true",
                          help="admit rows that do not declare themselves synthetic")
        part.add_argument("--out", default=None, help="write summary.json and trace.jsonl here")
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return EXIT_GO if exc.code in (0, None) else EXIT_USAGE
    try:
        lines, code = (do_gate if args.verb == "gate" else do_explain)(args)
    except G.GateError as exc:
        print(f"HALTED: {exc}")
        print("VERDICT: HOLD (crash: GateError)")
        return EXIT_USAGE
    except Exception as exc:  # never let an unexpected failure read as a pass
        print(f"VERDICT: HOLD (crash: {type(exc).__name__})")
        return EXIT_USAGE
    print("\n".join(lines))
    return code


if __name__ == "__main__":
    sys.exit(main())
