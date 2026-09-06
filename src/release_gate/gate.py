"""Rubric scores in, HOLD or GO out, with every threshold defended rather than asserted.

Standard library only. No network path exists in this package and no model is called. Every
number in a policy file is a design constant invented for this repository, not a validated
operating point, and each one carries its own rationale string that the receipt prints back.

The load-bearing idea: a floor is cleared only when the resampled lower bound clears it. A mean
that sits above the floor with a lower bound below it is reported FLOOR_UNCERTAIN, not OK -- the
release is held because the evidence is thin, and the receipt says so in those words.
"""
from __future__ import annotations

import hashlib
import json
import random
from collections import Counter
from pathlib import Path

VERSION = "0.1.0"
SCORE_MIN = 0.0
SCORE_MAX = 4.0
CHECKS = ("MIN_N", "FLOOR", "REGRESSION", "AGREEMENT", "SLICES")
ADMISSION_CODES = ("FIELD_MISSING", "NOT_SYNTHETIC", "UNKNOWN_CRITERION",
                   "SCORE_OUT_OF_RANGE", "DUPLICATE_ROW")


class GateError(Exception):
    """Any condition that stops the run. The caller prints HOLD and exits non-zero."""


def num(value) -> str:
    """A threshold as the policy would write it: 3.0 stays 3.0 and 3.9 stays 3.9."""
    return str(float(value))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_sha(obj) -> str:
    """sha256 over canonical key-sorted JSON, so a hash means the content and not the layout."""
    return sha256_bytes(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _read(path: str) -> tuple[str, str]:
    handle = Path(path)
    if not handle.is_file():
        raise GateError(f"file not found: {handle.name}")
    return handle.read_text(encoding="utf-8"), sha256_bytes(handle.read_bytes())


def load_policy(path: str) -> dict:
    text, sha = _read(path)
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise GateError(f"policy is not valid JSON: {exc.msg}") from exc
    for key in ("criteria", "regression_budget", "min_n", "min_agreement", "slices", "resample"):
        if key not in doc:
            raise GateError(f"policy is missing '{key}'")
    if not doc["criteria"]:
        raise GateError("policy declares no criteria")
    for name, spec in doc["criteria"].items():
        if "floor" not in spec or "rationale" not in spec:
            raise GateError(f"criterion {name} needs both a floor and a rationale")
    doc["sha256"] = sha
    return doc


def thresholds(policy: dict, overrides: dict | None = None) -> tuple[dict, dict]:
    """Return (effective thresholds, the policy's own values) so an override can be printed."""
    table = {f"floor.{name}": float(spec["floor"]) for name, spec in policy["criteria"].items()}
    table["budget"] = float(policy["regression_budget"]["value"])
    table["min_n"] = float(policy["min_n"]["value"])
    table["min_agreement"] = float(policy["min_agreement"]["value"])
    table["slice_min_n"] = float(policy["slices"]["min_n"])
    declared = dict(table)
    for key, value in (overrides or {}).items():
        if key not in table:
            raise GateError(f"--set names a threshold this policy does not have: {key}")
        table[key] = float(value)
    return table, declared


def rationale(policy: dict, key: str) -> str:
    if key.startswith("floor."):
        return policy["criteria"][key.split(".", 1)[1]]["rationale"]
    holder = {
        "budget": policy["regression_budget"],
        "min_n": policy["min_n"],
        "min_agreement": policy["min_agreement"],
        "slice_min_n": policy["slices"],
    }[key]
    return holder["rationale"]


def row_codes(row, policy: dict, seen: set, allow_nonsynthetic: bool) -> list[str]:
    """Fail-closed admission. A row that cannot be checked is refused, never averaged in."""
    fields = ("item", "criterion", "scores", "slice")
    if not isinstance(row, dict) or not all(key in row for key in fields):
        return ["FIELD_MISSING"]
    codes: list[str] = []
    if not allow_nonsynthetic and row.get("synthetic") is not True:
        codes.append("NOT_SYNTHETIC")
    if row["criterion"] not in policy["criteria"]:
        codes.append("UNKNOWN_CRITERION")
    scores = row["scores"]
    if not isinstance(scores, list) or not scores or not all(
        isinstance(s, (int, float)) and not isinstance(s, bool) and SCORE_MIN <= s <= SCORE_MAX
        for s in scores
    ):
        codes.append("SCORE_OUT_OF_RANGE")
    if (row["item"], row["criterion"]) in seen:
        codes.append("DUPLICATE_ROW")
    return codes


def load_rows(path: str, policy: dict, allow_nonsynthetic: bool = False) -> tuple[list, list, str]:
    text, sha = _read(path)
    rows: list = []
    refusals: list = []
    seen: set = set()
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            refusals.append((number, "FIELD_MISSING"))
            continue
        codes = row_codes(row, policy, seen, allow_nonsynthetic)
        if codes:
            refusals.extend((number, code) for code in codes)
            continue
        seen.add((row["item"], row["criterion"]))
        rows.append(row)
    return rows, refusals, sha


def mean(values) -> float:
    values = list(values)
    return sum(values) / len(values) if values else 0.0


def criterion_seed(seed: int, criterion: str) -> int:
    """A per-criterion seed derived from the criterion name, so two criteria do not share draws
    and the derivation does not depend on the interpreter's hash randomisation."""
    return seed + int(hashlib.sha256(criterion.encode("utf-8")).hexdigest()[:8], 16)


def resample_lo95(values: list, seed: int, draws: int) -> float:
    """The 2.5th percentile of `draws` resampled means. Seeded, so two runs agree byte for byte."""
    if not values:
        return 0.0
    rng = random.Random(seed)
    size = len(values)
    means = sorted(mean(rng.choice(values) for _ in range(size)) for _ in range(draws))
    return means[int(0.025 * draws)]


def compute_stats(candidate: list, baseline: list, policy: dict) -> dict:
    seed = int(policy["resample"]["seed"])
    draws = int(policy["resample"]["draws"])
    per: dict = {}
    for name in policy["criteria"]:
        rows = [r for r in candidate if r["criterion"] == name]
        values = [mean(r["scores"]) for r in rows]
        base = [mean(r["scores"]) for r in baseline if r["criterion"] == name]
        per[name] = {
            "n": len(rows),
            "mean": mean(values),
            "lo95": resample_lo95(values, criterion_seed(seed, name), draws),
            "baseline_n": len(base),
            "delta": (mean(values) - mean(base)) if (values and base) else None,
            "agreement": mean(1.0 if len(set(r["scores"])) == 1 else 0.0 for r in rows),
            "rows_sha256": canonical_sha(sorted(r["item"] for r in rows)),
        }
    slices = {
        name: len({r["item"] for r in candidate if r.get("slice") == name})
        for name in policy["slices"]["required"]
    }
    return {"criteria": per, "slices": slices, "seed": seed, "draws": draws, "rows": len(candidate)}


def evaluate(stats: dict, table: dict, checks=CHECKS) -> list:
    """Every HOLD reason, in a fixed order. `checks` is the injection seam the falsifier test
    uses to switch one check off and watch a real defect walk through."""
    reasons: list = []
    for name, s in stats["criteria"].items():
        floor = table[f"floor.{name}"]
        if "MIN_N" in checks and s["n"] < table["min_n"]:
            reasons.append(f"INSUFFICIENT_N:{name}")
        if "FLOOR" in checks:
            if s["mean"] < floor:
                reasons.append(f"FLOOR_MISSED:{name}")
            elif s["lo95"] < floor:
                reasons.append(f"FLOOR_UNCERTAIN:{name}")
        if "REGRESSION" in checks:
            if s["delta"] is None:
                reasons.append(f"BASELINE_MISSING:{name}")
            elif s["delta"] < table["budget"]:
                reasons.append(f"REGRESSION:{name}")
        if "AGREEMENT" in checks and s["agreement"] < table["min_agreement"]:
            reasons.append(f"JUDGE_DISAGREEMENT:{name}")
    if "SLICES" in checks:
        for name, count in stats["slices"].items():
            if count < table["slice_min_n"]:
                reasons.append(f"SLICE_UNCOVERED:{name}")
    return reasons


def flip_point(stats: dict, table: dict, key: str, checks=CHECKS):
    """The nearest value of one threshold that flips the verdict, found by re-deciding against
    already-computed statistics. Nothing is resampled here, so this is cheap and exact."""
    current = table[key]
    now = bool(evaluate(stats, table, checks))
    step = 1.0 if key in ("min_n", "slice_min_n") else 0.01  # both are counts of items
    for tick in range(1, 801):
        for value in (round(current + tick * step, 4), round(current - tick * step, 4)):
            if value < 0 and key != "budget":
                continue
            probe = dict(table)
            probe[key] = value
            if bool(evaluate(stats, probe, checks)) != now:
                return value
    return None


def _format_delta(delta) -> str:
    return "MISSING" if delta is None else f"{delta:+.2f}"


def _plural(count: int) -> str:
    return "reason" if count == 1 else "reasons"


def gate_lines(policy, stats, table, declared, reasons, refusals) -> list:
    read = stats["rows"] + len(refusals)
    out = [f"ADMISSION: {read} rows read  admitted={stats['rows']} refused={len(refusals)}"]
    if refusals:
        counts = Counter(code for _line, code in refusals)
        out.append("REFUSALS: " + " ".join(f"{c}={n}" for c, n in sorted(counts.items())))
    out.append(
        f"PER-CRITERION  (n = scored items; lo95 = resampled lower bound, "
        f"seed={stats['seed']}, draws={stats['draws']})"
    )
    for name, s in stats["criteria"].items():
        marks = [r.split(":")[0] for r in reasons if r.endswith(":" + name)]
        out.append(
            f"  {name:<12} n={s['n']} mean={s['mean']:.2f} lo95={s['lo95']:.2f} "
            f"floor={table['floor.' + name]:.2f} delta={_format_delta(s['delta'])} "
            f"budget={table['budget']:.2f} agree={s['agreement']:.2f} "
            f"min={table['min_agreement']:.2f}  " + (" ".join(marks) if marks else "OK")
        )
    covered = " ".join(f"{k} n={v}" for k, v in stats["slices"].items())
    missed = [r for r in reasons if r.startswith("SLICE_UNCOVERED")]
    out.append(
        f"SLICES: {covered} (min {int(table['slice_min_n'])} each)  "
        + (" ".join(missed) if missed else "OK")
    )
    changed = [
        f"{k}={num(table[k])} (policy: {num(declared[k])})" for k in table if table[k] != declared[k]
    ]
    out.append("OVERRIDES: " + (" ".join(changed) if changed else "none"))
    if reasons:
        out.append("REASONS: " + " ".join(reasons))
        out.append(f"VERDICT: HOLD ({len(reasons)} {_plural(len(reasons))})")
    else:
        out.append("VERDICT: GO")
    return out


def explain_lines(policy, table, stats) -> list:
    out = ["THRESHOLDS (design constants for this repository, not validated operating points)"]
    verdict = None
    if stats is not None:
        reasons = evaluate(stats, table)
        verdict = "HOLD" if reasons else "GO"
        out.append(f"CURRENT VERDICT on the given inputs: {verdict} ({len(reasons)} {_plural(len(reasons))})")
    for key in table:
        if stats is None:
            flip = "flip: MISSING (pass --baseline and --candidate to compute it)"
        else:
            point = flip_point(stats, table, key)
            other = "GO" if verdict == "HOLD" else "HOLD"
            flip = (
                "flip: none within 8.00 of the current value"
                if point is None
                else f"flip: {verdict} -> {other} at {key}={num(point)} "
                f"(change {point - table[key]:+.2f})"
            )
        out.append(f"  {key}={num(table[key])}  {flip}")
        out.append(f"      rationale: {rationale(policy, key)}")
    return out


def receipt(stats, table, declared, reasons, refusals, shas) -> tuple:
    """Counts, codes, verdict, hashes, seed, version -- and no score value anywhere."""
    summary = {
        "version": VERSION,
        "verdict": "HOLD" if reasons else "GO",
        "reasons": reasons,
        "reason_count": len(reasons),
        "codes": dict(sorted(Counter(r.split(":")[0] for r in reasons).items())),
        "rows_admitted": stats["rows"],
        "rows_refused": len(refusals),
        "refusal_codes": dict(sorted(Counter(c for _line, c in refusals).items())),
        "criteria_n": {name: s["n"] for name, s in stats["criteria"].items()},
        "slice_items": stats["slices"],
        "resample": {"seed": stats["seed"], "draws": stats["draws"]},
        "overrides": {k: num(table[k]) for k in table if table[k] != declared[k]},
        "inputs": shas,
    }
    trace = [
        {
            "criterion": name,
            "n": s["n"],
            "codes": [r.split(":")[0] for r in reasons if r.endswith(":" + name)],
            "rows_sha256": s["rows_sha256"],
        }
        for name, s in stats["criteria"].items()
    ]
    trace += [
        {
            "slice": name,
            "items": count,
            "codes": [r.split(":")[0] for r in reasons if r == f"SLICE_UNCOVERED:{name}"],
        }
        for name, count in stats["slices"].items()
    ]
    return summary, trace


def write_receipt(out_dir: str, summary: dict, trace: list) -> None:
    folder = Path(out_dir)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )
    (folder / "trace.jsonl").write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in trace),
        encoding="utf-8",
        newline="\n",
    )
