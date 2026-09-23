# Novelty and Prior-Art Analysis

## A note on how this analysis was produced — read this before anything else

This analysis was written **without live access to academic databases, patent
databases, or web search** — the build environment's network access is
restricted to software package registries (PyPI, npm, GitHub) and does not
extend to Google Scholar, USPTO/EPO/WIPO search tools, IEEE Xplore, ACM
Digital Library, or general web search. Everything below is therefore drawn
from general, pre-existing domain knowledge about well-known technologies and
categories of commercial product, **not from a systematic search performed for
this project**.

This means: **this document is not a prior-art search.** It is, at best, an
informed starting sketch of the landscape a real prior-art search would need
to examine. Treat every claim below as provisional and unverified until a
qualified search is actually performed. No specific paper, patent number, or
product feature list is cited here, because none was actually looked up —
inventing citations would be worse than declining to cite.

---

## What is not novel, and should not be claimed as novel

The individual mechanisms used throughout ServiceMesh are all well established
in the distributed-systems and enterprise-integration literature and in
commercial practice:

- **The Saga pattern** (a sequence of local transactions with compensating
  actions for a distributed transaction that cannot use two-phase commit) —
  a long-standing pattern, predating this project by decades, with extensive
  treatment in distributed-systems literature and in commercial workflow
  engines (Temporal, Camunda, AWS Step Functions, and others).
- **Idempotency keys for safe retries of mutating HTTP operations** — a
  standard, widely documented pattern (e.g. in payment-processing APIs) long
  before this project.
- **Retry with exponential backoff** — textbook material.
- **API gateways / anti-corruption layers translating between heterogeneous
  external contracts and an internal canonical model** — a named pattern in
  enterprise integration and domain-driven design.
- **Policy engines evaluating versioned business rules against a context** —
  the general shape of Open Policy Agent and numerous internal rules engines
  predating this project.
- **Constraint filtering followed by weighted multi-criteria scoring for
  resource/provider selection** — standard operations-research and allocation
  technique.
- **Event-driven architectures with a transactional outbox and idempotent
  consumers** — a named, well-documented pattern (the "Transactional Outbox"
  pattern).
- **Workflow orchestration engines generally** — an entire commercial
  category (Temporal, Camunda, AWS Step Functions, Azure Logic Apps, and
  others) exists to execute durable, resumable, multi-step processes.
- **Warranty management and field-service management as a software
  category** — an established commercial category with many vendors.
- **Enterprise Service Bus / integration platforms translating between
  heterogeneous partner APIs** — an established commercial and academic
  category (enterprise application integration, EAI).

None of the above should be presented, in a paper, a patent filing, or a
pitch deck, as something this project invented. Doing so would be both
inaccurate and easily refuted by anyone familiar with the space.

---

## The candidate contribution — stated as a hypothesis to test, not a fact

The specific thing worth investigating, subject to a real search, is whether
the following **combination** — not any individual piece — is treated as one
unified abstraction elsewhere in the specific domain of cross-organizational
consumer-electronics after-sales coordination:

1. A **Service Transaction** as a first-class, persisted object representing
   one customer request across multiple *independently governed, externally
   owned* organizations (not multiple internal microservices under one
   company's control, which is the more common setting for Saga/workflow
   literature and tooling).
2. An explicit **`state` vs `resume_state` separation**, where the recovery
   mechanism's resumption point is a distinct, independently-tracked field
   from the transaction's momentary status — as opposed to the more common
   pattern of either (a) restarting a failed workflow from its beginning, or
   (b) resuming from "wherever the workflow engine's internal execution
   pointer happens to be," without a domain-level distinction between
   "current status" and "last safe milestone."
3. A **three-way failure classification** (`TRANSIENT` / `PERMANENT` /
   `UNKNOWN`) used as the single decision point driving recovery strategy,
   where `UNKNOWN` specifically represents an unresolved side-effect outcome
   requiring active reconciliation via an idempotency key — as opposed to the
   more common binary framing of failures as simply "retry" or "give up."
4. **Federated policy, compatibility and provider-selection decisions**, each
   independently persisted with full inputs and rationale, applied *across*
   organization boundaries where no single organization has authority over
   the whole decision (the OEM decides authorization, the warranty provider
   decides coverage, ServiceMesh's own engine decides component compatibility
   and provider ranking, and none of these three parties could make the
   others' decisions).

**This is a hypothesis, not a finding.** Establishing whether this
combination is genuinely absent from the prior art requires:

- a systematic search of enterprise integration and distributed-systems
  literature (workflow orchestration, Saga pattern implementations, business
  transaction models),
- a search of commercial platform documentation (Temporal, Camunda, MuleSoft,
  Boomi, and other iPaaS/EAI platforms; ServiceNow, Salesforce Field Service,
  and other field-service platforms; warranty-management SaaS products),
- a patent search (USPTO, EPO, WIPO) for prior filings covering
  cross-organizational transaction state models, federated policy evaluation,
  or failure classification schemes with similar three-way semantics,
- and, importantly, a search specifically for whether any of the above
  platforms already describe a "state vs. last-safe-checkpoint" distinction
  under different terminology — this specific idea is simple enough that it
  may well already exist under a different name (e.g. "checkpoint state,"
  "last known good state," "compensation boundary") in workflow-engine
  documentation this analysis did not have access to search.

None of this searching has been done for this project. The statement above
should be read as "here is what a search would need to check," not "here is
what a search found."

---

## Closest prior-art categories (by name, not by specific citation)

Categories that a real search should examine first, in rough order of
likely relevance:

1. **Durable workflow engines** (Temporal, Camunda, AWS Step Functions) —
   these solve durable, resumable execution generally, and likely already
   have *some* notion of checkpointing distinct from "current activity."
   Whether any expose it as a domain-visible `resume_state` concept the way
   this project does, or leave it purely as an internal execution-engine
   detail, is exactly the kind of distinction a real search needs to resolve.
2. **Saga orchestration frameworks and papers** — the compensating-transaction
   half of this design is squarely inside this literature; the federated
   authority model (§4 above) is the part that would need to be shown as
   additional.
3. **Field-service management and warranty platforms** (as a commercial
   category) — these solve the domain problem (service centres, parts,
   warranty checks) but, based on general familiarity with the category
   rather than a specific search, more commonly integrate with a single
   OEM/retailer's own systems rather than modelling a transaction that spans
   multiple *externally owned* organizations as co-equal parties with none in
   overall control.
4. **API gateway / iPaaS platforms** (MuleSoft, Boomi, and similar) — solve
   the adapter/anti-corruption-layer problem generally; whether any expose a
   business-transaction abstraction with the specific failure/recovery
   semantics here is unexamined.
5. **Distributed transaction patent filings** — an area with substantial
   existing patent activity around Saga-style compensation, retry/backoff, and
   idempotency; a real search must be run against this corpus specifically
   before any patentability claim is even considered.

---

## Explicit statement on patentability

**No patentability claim is made or implied by this project.** See
`docs/patent-analysis.md` for the fuller treatment, but the short version:
professional patent counsel and a formal prior-art/freedom-to-operate search
are required before any filing decision, and this document does not
substitute for either.

---

## What would need to happen before any novelty or patent claim could be made responsibly

1. A structured literature search (ACM DL, IEEE Xplore, Google Scholar) using
   terms including: "federated saga," "cross-organizational business
   transaction," "distributed transaction state resumption," "failure
   classification recovery orchestration," "after-sales service
   coordination platform."
2. A structured patent search (USPTO full-text, EPO Espacenet, WIPO
   PatentScope) using similar terms plus classification codes for
   distributed transaction processing (e.g. G06Q, G06F sub-classes commonly
   used for business-process and transaction-processing patents — the exact
   classification would need to be determined with a patent professional's
   help, not guessed at here).
3. A review of the technical documentation of at least the commercial
   platforms named in §"Closest prior-art categories" above, specifically
   looking for whether any already publish a resumption-point concept
   equivalent to `resume_state`.
4. Engagement of a patent attorney or agent for a professional opinion on
   patentability and freedom-to-operate, which this document cannot provide.

Until that work is done, the correct and honest position is: **this project
combines well-known individual techniques in a way that may or may not be
novel in its specific combination and domain application, and that question
is open.**
