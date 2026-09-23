# Research Material

This document separates **design proposals** from **experimentally supported
claims**, as required. Anything not marked as measured is a design rationale,
not a result.

---

## Working title

*ServiceMesh: A Federated Service Transaction Abstraction for Failure-Aware
Cross-Organizational After-Sales Coordination*

## Abstract (draft)

Consumer-electronics after-sales service frequently requires coordination
across independently governed organizations — a marketplace, a manufacturer,
a warranty provider, a service centre and a parts supplier — each with its own
database, API, authentication scheme, and failure behaviour. Existing
integration approaches (point-to-point calls, workflow engines, enterprise
service buses) address the mechanics of calling multiple APIs but do not treat
the customer's request as a single first-class, stateful, resumable business
transaction with explicit failure classification and recovery semantics. We
present ServiceMesh, a system that models such a request as a **Service
Transaction**: a persisted state machine distinguishing "current state" from
"last safely completed state," combined with a deterministic recovery engine
that classifies failures as transient, permanent or of unknown outcome and
selects among retry, fallback-provider substitution, alternative-component
substitution, Saga-style compensation, or human escalation accordingly. We
implement the system against five independently-databased simulated
organizations, evaluate it against a conventional sequential-integration
baseline under a synthetic, controlled-failure workload, and report where the
evidence supports the design and where it does not. We do not claim the
individual mechanisms (Saga compensation, idempotency, retries, policy
engines) are novel — they are well established — and instead identify the
candidate contribution as the specific **combination and application** of
these mechanisms into one federated Service Transaction abstraction, subject
to the prior-art and novelty caveats in `docs/novelty.md`.

**Keywords**: distributed transactions, Saga pattern, idempotency,
after-sales service, enterprise integration, cross-organizational workflow,
failure-aware recovery.

---

## Problem statement

See README §"The problem" for the full framing. In research terms: existing
patterns for cross-organizational coordination (Saga, compensating
transactions, API gateways, workflow orchestration engines) are each
individually well studied, but the literature and commercial platforms
reviewed in `docs/novelty.md` treat *the specific combination* of (a) a
persisted, resumable business-transaction abstraction spanning independently
governed parties, (b) three-way failure classification driving deterministic
recovery, and (c) federated policy/compatibility/provider decisions with
persisted, auditable rationale, as either absent or not explicitly unified
under one abstraction in the after-sales domain specifically.

## Research gap

The gap under investigation, not yet validated by a systematic literature
search: general-purpose workflow and Saga frameworks provide the mechanical
primitives (compensating actions, retries, state persistence) but leave the
*domain semantics* — what counts as a permanent vs. transient failure in this
domain, what a "provider" and "component" substitution means, what
constitutes an auditable policy decision for warranty/compatibility — to the
integrator. Field-service and warranty-management platforms provide the
domain semantics but, per public documentation reviewed for `docs/novelty.md`,
do not describe a first-class distributed-transaction abstraction spanning
externally owned organizations with the failure/recovery matrix presented
here.

## Proposed architecture

Documented fully in `docs/architecture.md`. Summary of the model-level
contributions being investigated:

1. **The Service Transaction abstraction** — `state` vs `resume_state`
   separation as the mechanism for resumability without restart.
2. **Three-way failure classification** (`TRANSIENT` / `PERMANENT` /
   `UNKNOWN`) as the single decision point that determines whether retry,
   fallback, or reconciliation applies — with `UNKNOWN` specifically handling
   the case where a mutating call's outcome cannot be inferred from the
   client side.
3. **A recovery engine as an explicit, versioned, ordered rule table** (R1–R8)
   rather than ad hoc exception handling scattered through orchestration code.
4. **Persisted, replayable decision records** for policy, selection and
   compatibility, each carrying the exact inputs used, so a decision remains
   explicable after the fact even if the rules later change.

## Experimental methodology

Implemented in `experiments/`:

- `experiments/baseline.py` — a conventional sequential point-to-point
  integration, implemented honestly (same adapters, same organizations, same
  step order, same retry budget) but lacking persisted state, stable
  idempotency keys, failure classification, fallback selection and
  compensation. See the module docstring for the explicit list of what it
  deliberately lacks and why each omission is representative rather than a
  strawman.
- `experiments/run_experiment.py` — for each of N trials at each injected
  failure rate f ∈ {0, 0.3, 0.6}, runs the identical customer request through
  both systems against the same five organizations, with failures injected
  via the project's own simulation framework (a mix of `TEMPORARILY_UNAVAILABLE`,
  `TIMEOUT`, and `TIMEOUT_AFTER_COMMIT` across the marketplace, service centre
  and parts supplier). All six databases are reseeded between every single
  run so no trial inherits state from a previous one.
- `experiments/plots.py` — generates comparison charts directly from measured
  `summary.csv`; will not run against missing data.

### Metrics collected

`completion_rate`, `handled_without_human_rate` (resolved + cleanly rejected,
i.e. correct terminal outcomes not requiring a human), `manual_intervention_rate`,
`duplicate_side_effects_total` (**measured at the organizations' own
databases**, not inferred from the caller's retry count), `workflow_restarts_total`,
`avg_api_calls`, `avg_elapsed_seconds`, `recovery_actions_total`,
`recovery_success_rate`.

### Results (actual, measured; 60 runs total)

| f | System | Completion | Handled w/o human | Manual | Duplicates | Restarts | API calls |
|---|---|---|---|---|---|---|---|
| 0.0 | baseline | 0.20 | 0.40 | 0.60 | 0 | 12 | 13.6 |
| 0.0 | ServiceMesh | 0.80 | 1.00 | 0.00 | 0 | 0 | 11.2 |
| 0.3 | baseline | 0.20 | 0.40 | 0.60 | 4 | 12 | 15.6 |
| 0.3 | ServiceMesh | 0.80 | 1.00 | 0.00 | 0 | 0 | 13.7 |
| 0.6 | baseline | 0.20 | 0.40 | 0.60 | 2 | 12 | 16.8 |
| 0.6 | ServiceMesh | 0.80 | 1.00 | 0.00 | 0 | 0 | 15.2 |

Raw trial-level data: `experiments/results/trials.csv`. Full JSON with
hypothesis evaluation: `experiments/results/results.json`.

### Discussion — read the flat baseline curve carefully

The baseline's completion rate is **flat at 0.20 across every injected
failure rate, including zero**. This is the single most important thing to
notice in the data, and a naive reading of the headline completion-rate gap
(0.20 vs 0.80) as "ServiceMesh recovers from failures better" would be
**wrong, or at least incomplete**. Because the gap does not grow with the
failure rate, it is not primarily a failure-recovery effect — it is
substantially explained by a *different* capability: fallback provider/supplier
selection, which matters even at f=0 because the synthetic workload
deliberately includes a stockout at the nearest supplier
(`BAT-AX15-A` at `SUP-A`, see `scripts/dataset.py`) and a
regionally-nearest-but-unauthorized service centre for one customer
(`SVC-NAG-GREY`). The baseline's "first provider, no fallback" design fails
these regardless of whether any failure is injected.

`experiments/results/summary.csv` includes an `outcome_breakdown` column
making this explicit per condition (e.g.
`no_stock_no_fallback=2; provider_unauthorized_no_fallback=1; resolved=2`),
and `run_experiment.py` prints this caveat next to the H1 result rather than
letting the headline number stand alone.

**What would isolate the recovery effect specifically**: an experiment
holding provider/component availability constant (so fallback capability is
never exercised) and varying only the injected transient-failure rate. That
experiment has not been run. Until it is, H1 should be read as "ServiceMesh's
combination of capabilities completes more transactions under this workload,"
not "ServiceMesh's recovery engine specifically causes this gap."

### Hypotheses — reported honestly, including the ones needing more evidence

| ID | Statement | Result | Support strength |
|---|---|---|---|
| H1 | A stateful failure-aware Service Transaction model improves completion under partial failure vs. a sequential workflow | 0.20 → 0.80 | **Supported for the combined system; NOT isolated to the recovery mechanism specifically — see discussion above.** |
| H2 | Automated recovery reduces manual intervention | 0.60 → 0.00 | Supported. Every baseline failure path in this workload terminates in `MANUAL_INTERVENTION` because it has no fallback; ServiceMesh's fallback and escalation logic resolves or cleanly rejects instead. |
| H3 | Idempotency reduces duplicate business operations | 6 total → 0 total | Supported, and measured at the organizations' own databases rather than inferred. |
| H4 | Constraint-aware provider selection improves SLA performance | *not measured in this run* | Not evaluated — no experiment varying SLA constraints specifically has been run. Listed as a hypothesis still requiring its own experiment. |
| H5 | Persisted transaction state reduces unnecessary workflow restarts | 36 total → 0 total | Supported by construction: ServiceMesh's `resume_state` mechanism is defined never to restart; the baseline's in-memory-only progress is defined to always restart on exhausted retries. This is closer to a structural consequence of the two designs than an emergent experimental finding, and is reported as such. |

### Limitations of this evaluation

- **Sample size is small** (10 trials × 3 failure rates × 2 systems = 60 runs)
  and the workload draws from 5 pinned scenario fixtures cycled repeatedly,
  not a large randomly generated population.
- **The workload is synthetic**, generated by this project's own dataset and
  simulation framework. It characterises this implementation under this
  controlled workload and must not be read as an industry benchmark.
- **H1's headline number is confounded** with fallback-selection capability,
  as discussed above.
- **H4 is untested.** It is listed here as a hypothesis for future work, not
  quietly dropped.
- **No statistical significance testing** (confidence intervals, hypothesis
  tests) has been applied to the differences reported; at n=10 per condition
  this would be advisable before treating any gap as more than suggestive.

---

## What is a design proposal, not a validated finding

To be explicit about the boundary the brief asks for:

**Validated by experiment** (with the caveats above): the completion-rate,
manual-intervention, duplicate-operation, and restart-count comparisons in
the table.

**Design proposals, not (yet) experimentally validated**:
- That the three-way failure classification generalises usefully beyond this
  domain's five organization types.
- That the recovery rule table (R1–R8) is complete or optimal — it is
  deterministic and testable, not proven exhaustive.
- H4 (SLA performance under constraint-aware selection).
- Any claim about performance, cost, or completion rate under a real
  (non-synthetic) transaction volume or a real organizational failure
  distribution.
- Any claim of novelty — see `docs/novelty.md`, which treats novelty
  explicitly as an open question requiring a prior-art search not yet
  performed.

---

## Reproducing these results

```bash
python -m experiments.run_experiment --trials 10 --failure-rates 0.0 0.3 0.6 --seed 42
python -m experiments.plots
```

The `--seed` flag makes the injected-failure pattern and workload ordering
deterministic; re-running with the same seed reproduces the same trial-level
outcomes (subject to any timing-dependent behaviour in the simulated
`HIGH_LATENCY`/`TIMEOUT` modes, which is not used in the default failure-mode
mix for this experiment).
