"""Every rule the gate enforces, plus the two tests that matter most: the seeded-bad fixture the
gate MUST refuse, and the falsifier that switches one check off and watches a real defect walk
straight through. A gate that has never been shown to fail certifies nothing.
"""
from __future__ import annotations

import contextlib
import io
import re
from pathlib import Path

from release_gate import cli
from release_gate import gate as G

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "fixtures"
POLICY = str(FIX / "policy.json")
BASE = str(FIX / "baseline.jsonl")
GO = str(FIX / "candidate_go.jsonl")
HOLD = str(FIX / "candidate_hold.jsonl")
ANCHOR = "PUBLIC-CLEAN-PATTERN-SOURCE"

FORBIDDEN = (  # PUBLIC-CLEAN-PATTERN-SOURCE
    ("drive_letter_root", re.compile(r"(?<![A-Za-z])[A-Za-z]:[\\/]")),  # PUBLIC-CLEAN-PATTERN-SOURCE
    ("windows_user_home", re.compile(r"[\\/]Users[\\/]", re.I)),  # PUBLIC-CLEAN-PATTERN-SOURCE
    ("posix_user_home", re.compile(r"/home" r"/[a-z0-9._-]+", re.I)),  # PUBLIC-CLEAN-PATTERN-SOURCE
    ("accuracy_claim", re.compile(r"\b(MAE|accuracy|band)\s*[:=]?\s*[0-9]", re.I)),  # PUBLIC-CLEAN-PATTERN-SOURCE
    ("personal_mailbox", re.compile(r"[a-z0-9._%+-]+@(gmail|outlook|yahoo|hotmail)\.", re.I)),  # PUBLIC-CLEAN-PATTERN-SOURCE
)
PLANTS = {  # each value is a shape the scan must catch; the anchor keeps the plant out of the scan
    "drive_letter_root": "opened D:" + "/results/run.jsonl",  # PUBLIC-CLEAN-PATTERN-SOURCE
    "windows_user_home": "saved to \\Users" + "\\someone\\out",  # PUBLIC-CLEAN-PATTERN-SOURCE
    "posix_user_home": "saved to /home" + "/someone/out",  # PUBLIC-CLEAN-PATTERN-SOURCE
    "accuracy_claim": "accuracy = 0.91 on the internal set",  # PUBLIC-CLEAN-PATTERN-SOURCE
    "personal_mailbox": "write to someone" + "@gmail.com",  # PUBLIC-CLEAN-PATTERN-SOURCE
}


def run(argv):
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = cli.main(argv)
    return buffer.getvalue(), code


def stats_for(candidate_rows, policy):
    baseline, _refusals, _sha = G.load_rows(BASE, policy)
    return G.compute_stats(candidate_rows, baseline, policy)


def rows_for(criterion, pairs, slice_name="short"):
    return [
        {"item": f"i{n}", "criterion": criterion, "scores": list(p), "slice": slice_name,
         "synthetic": True}
        for n, p in enumerate(pairs)
    ]


def test_go_fixture_passes_with_no_overrides():
    out, code = run(["gate", "--policy", POLICY, "--baseline", BASE, "--candidate", GO])
    assert code == 0 and out.rstrip().endswith("VERDICT: GO")
    assert "SLICES: short n=4 long n=4 (min 2 each)  OK" in out
    assert "OVERRIDES: none" in out
    assert out.count("  OK") == 4  # three criteria and the slice line


def test_hold_fixture_names_exactly_three_reasons():
    out, code = run(["gate", "--policy", POLICY, "--baseline", BASE, "--candidate", HOLD])
    assert code == 2
    assert "REASONS: FLOOR_MISSED:GROUNDING REGRESSION:GROUNDING SLICE_UNCOVERED:long" in out
    assert "VERDICT: HOLD (3 reasons)" in out


def test_live_threshold_override_flips_go_to_hold():
    out, code = run(["gate", "--policy", POLICY, "--baseline", BASE, "--candidate", GO,
                     "--set", "floor.SAFETY=3.9"])
    assert code == 2
    assert "OVERRIDES: floor.SAFETY=3.9 (policy: 3.0)" in out
    assert "REASONS: FLOOR_MISSED:SAFETY" in out and "VERDICT: HOLD (1 reason)" in out


def test_explain_prints_rationale_and_flip_point():
    out, code = run(["explain", "--policy", POLICY, "--baseline", BASE, "--candidate", GO])
    assert code == 0
    assert "floor.SAFETY=3.0  flip: GO -> HOLD at floor.SAFETY=3.32" in out  # the lower bound, not the mean
    assert out.count("rationale: ") == 7  # one per threshold this policy declares
    bare, _code = run(["explain", "--policy", POLICY])
    assert "flip: MISSING" in bare  # no candidate, so no flip point is invented


def test_floor_uncertain_when_lower_bound_straddles_floor():
    policy = G.load_policy(POLICY)
    table, _declared = G.thresholds(policy)
    thin = rows_for("CORRECTNESS", [(2, 2), (2, 2), (2, 2), (3, 3), (3, 3), (3, 3), (4, 4), (4, 4)])
    stats = stats_for(thin, policy)
    seen = stats["criteria"]["CORRECTNESS"]
    assert seen["mean"] >= table["floor.CORRECTNESS"] > seen["lo95"]
    reasons = G.evaluate(stats, table)
    assert "FLOOR_UNCERTAIN:CORRECTNESS" in reasons and "FLOOR_MISSED:CORRECTNESS" not in reasons


def test_insufficient_n_and_judge_disagreement_are_separate_reasons():
    policy = G.load_policy(POLICY)
    table, _declared = G.thresholds(policy)
    split = rows_for("SAFETY", [(3, 4)] * 8)
    thin = rows_for("SAFETY", [(4, 4)] * 4)
    assert "JUDGE_DISAGREEMENT:SAFETY" in G.evaluate(stats_for(split, policy), table)
    assert "INSUFFICIENT_N:SAFETY" in G.evaluate(stats_for(thin, policy), table)


def test_admission_refuses_five_shapes_fail_closed(tmp_path):
    good = Path(GO).read_text(encoding="utf-8")
    planted = good + "\n".join([
        '{"item": "itm-09", "criterion": "SAFETY", "scores": [7, 4], "slice": "short", "synthetic": true}',
        '{"item": "itm-09", "criterion": "STYLE", "scores": [3, 3], "slice": "short", "synthetic": true}',
        '{"item": "itm-01", "criterion": "SAFETY", "scores": [3, 3], "slice": "short", "synthetic": true}',
        '{"item": "itm-10", "criterion": "SAFETY", "scores": [3, 3], "slice": "short"}',
        '{"item": "itm-11", "criterion": "SAFETY", "scores": [3, 3]}',
    ]) + "\n"
    path = tmp_path / "planted.jsonl"
    path.write_text(planted, encoding="utf-8")
    policy = G.load_policy(POLICY)
    _rows, refusals, _sha = G.load_rows(str(path), policy)
    codes = {code for _line, code in refusals}
    assert codes == {"SCORE_OUT_OF_RANGE", "UNKNOWN_CRITERION", "DUPLICATE_ROW",
                     "NOT_SYNTHETIC", "FIELD_MISSING"}
    out, code = run(["gate", "--policy", POLICY, "--baseline", BASE, "--candidate", str(path)])
    assert code == 2 and "refused=5" in out and "VERDICT: HOLD" in out


def test_falsifier_regression_check_disabled_lets_hold_pass():
    """The only defect in this candidate is a regression. Switch the regression check off through
    the `checks=` seam and the gate says GO on a candidate it should have held."""
    policy = G.load_policy(POLICY)
    table, _declared = G.thresholds(policy)
    candidate, _refusals, _sha = G.load_rows(GO, policy)
    for row in candidate:
        if row["criterion"] == "GROUNDING":
            row["scores"] = [2, 2] if row["item"] == "itm-08" else [3, 3]
    stats = stats_for(candidate, policy)
    assert G.evaluate(stats, table) == ["REGRESSION:GROUNDING"]
    assert G.evaluate(stats, table, checks=tuple(c for c in G.CHECKS if c != "REGRESSION")) == []


def test_receipt_is_byte_identical_across_two_runs(tmp_path):
    for name in ("one", "two"):
        run(["gate", "--policy", POLICY, "--baseline", BASE, "--candidate", HOLD,
             "--out", str(tmp_path / name)])
    for artifact in ("summary.json", "trace.jsonl"):
        assert (tmp_path / "one" / artifact).read_bytes() == (tmp_path / "two" / artifact).read_bytes()


def test_trace_carries_ids_codes_counts_and_hashes_only(tmp_path):
    run(["gate", "--policy", POLICY, "--baseline", BASE, "--candidate", HOLD,
         "--out", str(tmp_path / "hold")])
    text = (tmp_path / "hold" / "trace.jsonl").read_text(encoding="utf-8")
    assert "FLOOR_MISSED" in text
    for word in ("mean", "lo95", "delta", "scores", "agreement"):
        assert word not in text
    assert not re.search(r"\d+\.\d+", text)  # no score value ever reaches the trace


def test_public_clean_scan_is_clean_and_its_planted_positives_fire():
    findings, scanned = [], 0
    for path in sorted(p for p in ROOT.rglob("*") if p.is_file()):
        if any(part in {".git", "__pycache__", ".pytest_cache", "runs"} for part in path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        scanned += 1
        for line in text.splitlines():
            if ANCHOR in line:
                continue
            findings += [f"{path.name}:{name}" for name, rx in FORBIDDEN if rx.search(line)]
    assert scanned >= 12, f"the scan reached {scanned} files; a scan that reads nothing reads clean"
    assert findings == []
    for name, rx in FORBIDDEN:  # a scan never shown to fail certifies nothing
        assert rx.search(PLANTS[name]), name


def test_no_network_import_in_src():
    text = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "src").rglob("*.py"))
    for banned in ("urllib", "socket", "http.client", "requests", "httpx"):
        assert banned not in text


def test_crash_prints_hold_before_exit_one():
    out, code = run(["gate", "--policy", str(FIX / "no_such_policy.json"),
                     "--baseline", BASE, "--candidate", GO])
    assert code == 1 and "VERDICT: HOLD (crash: GateError)" in out


def test_unknown_threshold_in_set_is_refused():
    out, code = run(["gate", "--policy", POLICY, "--baseline", BASE, "--candidate", GO,
                     "--set", "floor.STYLE=3.0"])
    assert code == 1 and "VERDICT: HOLD (crash: GateError)" in out
