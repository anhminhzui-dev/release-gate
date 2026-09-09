# release-gate

[![CI](https://github.com/anhminhzui-dev/release-gate/actions/workflows/ci.yml/badge.svg)](https://github.com/anhminhzui-dev/release-gate/actions/workflows/ci.yml)
[![Licence](https://img.shields.io/badge/licence-evaluation--only-blue)](LICENSE)

**A high average should not silently override the release policy.**

A deterministic decision gate for rubric-score fixtures. It admits baseline and candidate rows, then checks criterion floors using a seeded resampled lower bound alongside regression, sample-size, judge-agreement and slice-coverage requirements.

```text
policy + baseline + candidate → input admission → per-criterion resampling
                             → ordered gate reasons → GO / HOLD + receipt
```

The `explain` command identifies the nearest threshold change that flips the current verdict. Regression tests disable a guard to show the failure it prevents. Receipts retain counts, codes and hashes rather than individual judge scores.

The fixtures and thresholds are synthetic design choices. The resampled lower bound is a calculation, not evidence of calibrated confidence coverage, independent human agreement or production release safety. Commands below run locally without a model or network call.

## What it refuses

Admission — fail-closed, before anything is averaged. Any hit forces HOLD.

| Code | What triggers it |
|---|---|
| `SCORE_OUT_OF_RANGE` | a judge score outside the policy's 0–4 range, or not a number |
| `DUPLICATE_ROW` | the same item scored twice on the same criterion |
| `UNKNOWN_CRITERION` | a criterion the policy does not declare — refused, never guessed |
| `NOT_SYNTHETIC` | a row that does not declare itself synthetic (`--allow-nonsynthetic` to admit it) |
| `FIELD_MISSING` | a row missing item, criterion, scores or slice, or an unparseable line |

Gate — one reason per defended threshold.

| Code | What triggers it |
|---|---|
| `FLOOR_MISSED` | the criterion mean is below its floor |
| `FLOOR_UNCERTAIN` | the mean clears the floor but the resampled 95% lower bound does not — the threshold is not yet defended, so the release is held |
| `REGRESSION` | the mean moved against the baseline by more than the budget |
| `INSUFFICIENT_N` | fewer scored items than the policy's minimum |
| `SLICE_UNCOVERED` | a required slice carries fewer items than the minimum |
| `JUDGE_DISAGREEMENT` | exact agreement between judges below the minimum |
| `BASELINE_MISSING` | a criterion has no baseline rows, so no regression claim can be made |

## Run it

Standard library only, Python 3.10+. Nothing to install; `pytest` only for the tests.

The clean candidate — 24 rows of 24 admitted, 8 items of 8 per criterion, 2 judges per item:

```
$ PYTHONPATH=src python -m release_gate.cli gate --policy fixtures/policy.json \
    --baseline fixtures/baseline.jsonl --candidate fixtures/candidate_go.jsonl
ADMISSION: 24 rows read  admitted=24 refused=0
PER-CRITERION  (n = scored items; lo95 = resampled lower bound, seed=20260906, draws=2000)
  CORRECTNESS  n=8 mean=3.38 lo95=3.12 floor=2.50 delta=+0.12 budget=-0.25 agree=0.75 min=0.75  OK
  GROUNDING    n=8 mean=3.19 lo95=3.00 floor=2.50 delta=+0.00 budget=-0.25 agree=0.88 min=0.75  OK
  SAFETY       n=8 mean=3.62 lo95=3.31 floor=3.00 delta=+0.12 budget=-0.25 agree=0.75 min=0.75  OK
SLICES: short n=4 long n=4 (min 2 each)  OK
OVERRIDES: none
VERDICT: GO
$ echo $?
0
```

The seeded-bad candidate — same shape, GROUNDING pushed under both its floor and its budget, and
every item tagged `short` so the long slice is empty:

```
$ PYTHONPATH=src python -m release_gate.cli gate --policy fixtures/policy.json \
    --baseline fixtures/baseline.jsonl --candidate fixtures/candidate_hold.jsonl --out runs/hold
  GROUNDING    n=8 mean=2.38 lo95=2.12 floor=2.50 delta=-0.81 budget=-0.25 agree=0.75 min=0.75  FLOOR_MISSED REGRESSION
SLICES: short n=8 long n=0 (min 2 each)  SLICE_UNCOVERED:long
REASONS: FLOOR_MISSED:GROUNDING REGRESSION:GROUNDING SLICE_UNCOVERED:long
VERDICT: HOLD (3 reasons)
$ echo $?
2
```

`runs/hold/summary.json` carries the verdict, the codes, the row counts, the seed and the sha256
of all three inputs; `runs/hold/trace.jsonl` carries ids, codes, counts and hashes — no score value
reaches either file, and a test asserts it.

Change one threshold on the command line and the same clean candidate is held, with the change on
the record:

```
$ PYTHONPATH=src python -m release_gate.cli gate ... --candidate fixtures/candidate_go.jsonl \
    --set floor.SAFETY=3.9
OVERRIDES: floor.SAFETY=3.9 (policy: 3.0)
REASONS: FLOOR_MISSED:SAFETY
VERDICT: HOLD (1 reason)
```

`explain` prints every threshold, its rationale, and the smallest change that would flip the
verdict — 7 thresholds of 7:

```
$ PYTHONPATH=src python -m release_gate.cli explain --policy fixtures/policy.json \
    --baseline fixtures/baseline.jsonl --candidate fixtures/candidate_go.jsonl
  floor.SAFETY=3.0  flip: GO -> HOLD at floor.SAFETY=3.32 (change +0.32)
      rationale: Set above the other two on purpose: a safety miss is not tradeable against a
      correctness win, so this floor is the one that should hurt to move.
```

SAFETY's mean is 3.62 but the gate flips at 3.32, because 3.31 is the lower bound. That gap is the
whole argument of this repository.

Tests: `python -m pytest -q` → 14 passed of 14, including the seeded-bad fixture the gate must
refuse and `test_falsifier_regression_check_disabled_lets_hold_pass`, which switches the regression
check off through an injection seam and watches a real defect walk through — a gate never shown to
fail certifies nothing.

## Integration path

Read the last ten release decisions and ask which threshold each one turned on, and who could
name the rationale without looking. Most gates fail not because the metric is wrong but because
nobody can say what number would have changed the answer. So: write the policy down as a file with
a rationale per line, put the acceptance run in CI where it can block, and report every floor with
its lower bound so a thin sample is visible as thin rather than rounded into a pass. Then take the
first conversational and RAG suites and add the slice coverage check, because the number that
sinks a release is usually the one nobody sliced.

## Scope and integration

Synthetic rubric rows demonstrate validation, resampling and release decisions. The bootstrap bounds reproduce the stated calculation; they are not measurements of independent human-rater agreement or production calibration.

Thresholds and sampling assumptions must match the intended evaluation population. The package runs offline with the Python standard library and does not call a model or network service.

## Project context

Problem definition, architecture and acceptance review: **Minh Vo**, with AI-assisted implementation. This focused tool belongs to a broader body of data, assessment and training-systems work described in the [research overview](https://github.com/anhminhzui-dev/anhminhzui-dev#research-constructing-evidence-grounded-language-judgments). Its runnable scope is the mechanism documented here.

## Licence

Evaluation-Only Licence 1.0 — source-available, not open source. See `LICENSE`.
