# Patent-Oriented Analysis

## This is not legal advice, and this is not a patentability opinion

Nothing in this document should be relied upon as legal advice, and nothing
in it constitutes a patentability opinion. It is an engineering-level sketch
of what *might* be worth showing to a patent attorney, written by the
project's implementer without legal training and without access to patent
search tools (see `docs/novelty.md` for the access limitations under which
this was produced). **Professional patent counsel and a formal prior-art and
freedom-to-operate (FTO) search are required before any filing decision is
made**, and this document is explicitly not a substitute for either.

---

## Potentially differentiating technical aspects worth showing to counsel

These are described as *candidates for investigation*, not as claims of
patentable subject matter. Each is stated alongside the most obvious prior-art
category it would have to be distinguished from.

### Candidate 1: `state` / `resume_state` separation as a queryable domain field

**What it is**: `ServiceTransaction.state` and `ServiceTransaction.resume_state`
are two independently persisted, independently queryable fields. `state` may
be a transient control value (`RETRYING`, `WAITING`, `ESCALATED`); `resume_state`
only ever advances along the happy path and is what recovery reads to decide
where to continue. Both are visible in the API response and the audit trail,
not merely internal execution-engine bookkeeping.

**Nearest prior art to distinguish from**: workflow engines (Temporal,
Camunda) almost certainly have *some* internal notion of a durable execution
checkpoint — that is the core value proposition of "durable execution."
Whether any expose it as a first-class, separately-named, domain-visible field
distinct from the workflow's current activity — rather than an opaque
internal event-history pointer — is the specific question a patent search
needs to answer. If a competing system's checkpoint concept is purely an
implementation detail of its execution engine and not a modeled domain field
with its own semantics and API surface, that may be a point of distinction;
if any existing system already exposes an equivalent field under different
naming, this candidate is likely not novel.

### Candidate 2: three-way failure classification driving a deterministic recovery table, keyed specifically to operation mutability

**What it is**: failures are classified `TRANSIENT` / `PERMANENT` / `UNKNOWN`,
and the `UNKNOWN` classification is specifically triggered by "a timeout
occurred on an operation that is known, ahead of time, to mutate state at the
external organization" (`MUTATING_OPERATIONS` in `core/enums.py`) — not by a
generic ambiguous-error heuristic. The recovery table (R1–R8 in
`engines/recovery.py`) is then a deterministic function of this classification
plus a small number of other context fields (attempts remaining, alternative
availability, uncompensated side effects).

**Nearest prior art to distinguish from**: retry-with-backoff and
circuit-breaker patterns are extremely well established and almost certainly
already distinguish *some* notion of retryable vs. non-retryable errors (e.g.
HTTP 5xx vs 4xx is a common heuristic in existing retry libraries). The
specific, narrower question for a patent search is whether any existing
system ties the "unknown outcome" classification specifically to
*operation-type mutability known in advance* (as opposed to inferring
ambiguity from the error itself, e.g. "the connection dropped so we don't
know") and then mandates a *specific* reconciliation-via-idempotency-key
recovery step rather than a generic retry. This is a much narrower and more
specific claim surface than "we classify failures," and narrower claims are
generally easier to distinguish from prior art but also narrower in scope if
granted.

### Candidate 3: federated policy evaluation with no single authoritative party

**What it is**: a service-provider-authorization decision requires querying
the OEM (who has sole authority over authorization) while a *different*
decision — component compatibility — is made by ServiceMesh's own engine
using OEM-published data, and a *third* decision — coverage — requires
querying the warranty provider (who has sole authority over coverage). No
single party, including ServiceMesh itself, has authority over the whole
composite decision, and the system's policy engine explicitly models this by
querying different parties for different sub-decisions within one evaluation
(see `PolicyEngine.evaluate("PROVIDER_ELIGIBILITY", ...)` combining a local
rule with a live `provider_authorization` answer sourced from the OEM).

**Nearest prior art to distinguish from**: policy engines like OPA, and
federated-identity / federated-authorization concepts generally (e.g.
federated identity providers, cross-domain authorization in access-control
literature), already model "ask a different authority for different facts."
The question for a patent search is whether the specific pattern of combining
externally-sourced, per-organization authoritative answers with locally
computed decisions, all persisted with input provenance in one composite
decision record, is claimed anywhere in the business-process or
distributed-transaction patent literature specifically (as opposed to the
access-control/authorization literature, which is a different application
domain).

---

## What evidence would be required before filing

A patent attorney would typically want, at minimum:

1. **A completed prior-art and FTO search** covering the categories in
   `docs/novelty.md` — not the informal sketch in this document, but a
   professional search using patent classification codes and a proper patent
   database.
2. **Reduction to practice with dated evidence** — this repository, its
   git history (if committed with real timestamps), and the test suite serve
   as candidate evidence of conception and reduction to practice, but formal
   filing timelines (e.g., US provisional filing deadlines relative to any
   public disclosure) would need to be assessed by counsel given when and how
   this project might be publicly disclosed (a GitHub repo, a conference talk,
   a hackathon demo — each has different implications for a subsequent
   filing).
3. **Claim drafting** narrowing each candidate above to specific, defensible
   technical steps rather than the broad conceptual descriptions given here —
   patent claims live or die on precise language that only a patent
   professional should draft.
4. **A commercial rationale** for filing at all — patents are expensive to
   prosecute and maintain; whether it is worth pursuing depends on the
   business strategy in `docs/business.md`, not on technical novelty alone.

---

## Explicit disclaimer, restated

This document does not assert that any of the three candidates above are
patentable. It does not assert freedom to operate with respect to any
existing patent. It was produced without access to a patent search tool. Any
decision to pursue patent protection based on this project must go through
professional patent counsel and a properly conducted prior-art and FTO
search, neither of which has occurred here.
