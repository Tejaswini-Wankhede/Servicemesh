# Business and Product Positioning

## Problem and value proposition

Consumer-electronics after-sales service is fragmented across organizations
that do not share systems: a marketplace, an OEM, a warranty provider, a
network of service centres, and one or more parts suppliers. Today this is
typically coordinated by manual case management, ad hoc point-to-point
integrations, or a patchwork of spreadsheets and phone calls between partners.
The customer experiences this fragmentation directly — repeated
identity-proving, unclear status, and slow escalation when any one link in
the chain is unavailable.

ServiceMesh's value proposition, stated modestly and specifically to what is
actually built and demonstrated:

- **Reduced fragmentation**: one transaction, one correlation id, one
  customer-facing status, coordinating five otherwise-disconnected systems.
- **Increased visibility**: a full, persisted audit trail of every operation,
  failure, and decision — demonstrated on the Transaction Detail page and
  backed by real database rows, not a summary generated after the fact.
- **Reduced manual intervention on recoverable failures**: measured in
  `docs/research.md` at 0.60 → 0.00 manual-intervention rate in the specific
  synthetic workload tested (see that document's caveats before generalising
  this number).
- **Improved recovery from provider failures**: fallback provider and
  component substitution, demonstrated end-to-end.
- **An auditable cross-company transaction layer**: every policy,
  compatibility and provider-selection decision is persisted with its
  rationale and the exact inputs used, which is the kind of record a
  warranty dispute, a regulatory inquiry, or an OEM audit would need and which
  none of the five individual organizations' own systems would have in full
  (each only sees its own slice).

## Target customers and initial market

**Initial market: consumer electronics after-sales**, deliberately narrow
rather than "any industry" — the brief for this project explicitly warns
against overreach, and the domain-specific compatibility engine, warranty
coverage model, and component-substitution logic built here are genuinely
specific to this vertical.

**Target customer personas**:

1. **OEMs** who currently either build their own coordination layer in-house
   (expensive, slow to extend to new partners) or leave coordination to their
   service-centre network and warranty provider to sort out ad hoc (poor
   visibility, inconsistent customer experience).
2. **Warranty providers / extended-warranty administrators** who need to
   coordinate with service centres and parts suppliers they do not control,
   and who currently rely on manual case management for anything beyond the
   simplest claims.
3. **Service-network operators** (companies that manage a network of
   authorized repair centres on behalf of one or more OEMs) who need a
   single system of record across centres with different capabilities,
   capacities, and reliability.
4. **Enterprises operating multi-brand or multi-partner after-sales
   ecosystems** — retailers or marketplaces that sell many brands and need
   one coordination layer rather than one integration per OEM.

## Competitive positioning — stated honestly

**ServiceMesh does not have no competitors.** It would need to differentiate
against several existing categories:

| Category | Examples (by category, not asserting any specific vendor's exact feature set) | How ServiceMesh would need to differentiate |
|---|---|---|
| Enterprise integration platforms / iPaaS | MuleSoft, Boomi, Workato and similar | These solve the adapter/connectivity problem generally, well, and at scale. ServiceMesh's differentiation would have to be the domain-specific transaction model (state/resume_state, three-way failure classification, compatibility engine) — not the raw ability to call multiple APIs, which iPaaS platforms already do thoroughly. |
| Workflow orchestration engines | Temporal, Camunda, AWS Step Functions | These solve durable execution well and are more mature and battle-tested than ServiceMesh's current in-house state machine. ServiceMesh's differentiation is the domain model layered on top, not the execution engine itself — and as the README states, adopting one of these underneath ServiceMesh's domain layer is explicitly left as a future integration path, not a competitive moat. |
| Warranty management platforms | category includes various SaaS warranty/claims administration products | These solve the claims and coverage side well within one organization's control. ServiceMesh's differentiation would be genuine multi-organization, multi-authority coordination — the OEM, warranty provider and service network as co-equal external parties rather than modules under one vendor's platform. |
| Field-service management platforms | category includes various FSM SaaS products (scheduling, dispatch, technician management) | These solve the service-centre-side operational problem (scheduling, dispatch) very well. ServiceMesh does not attempt to replace this — a real deployment would need to *integrate* with a customer's existing FSM tooling as one of the "provider adapters," not replace it. |

**Where ServiceMesh would need real differentiation to win**: the specific
combination of (a) treating the cross-organization request as one persisted,
resumable business transaction rather than a sequence of API calls glued
together by a workflow engine, and (b) the domain-specific compatibility and
warranty-coverage logic. Neither of these is defensible on technical
sophistication alone against well-funded incumbents in the categories above;
the realistic path to differentiation is depth in this specific vertical
(consumer electronics after-sales) rather than breadth.

## Pricing possibilities (illustrative, not committed)

- **Per-transaction fee**, charged to the party that benefits most from
  coordination (commonly the OEM or the warranty provider), scaled by
  transaction complexity (number of organizations involved, whether recovery
  was invoked).
- **Platform subscription** per connected organization (a service-centre
  network operator paying per branch connected), which aligns incentives
  with network growth rather than per-transaction volume.
- **Hybrid**: a base platform fee plus usage-based transaction pricing,
  common in integration-platform pricing generally.

These are illustrative starting points for discussion, not a costed pricing
model — no unit-economics analysis has been performed.

## Enterprise deployment model

Each simulated organization in this repository stands in for what would, in a
real deployment, be a **connector to that organization's existing system** —
their own order management system, their own warranty/claims system, their
own dispatch/FSM tooling, their own inventory system — via a
`ProviderAdapter` written for their actual API. The architecture is built for
this: replacing a simulated organization means writing one adapter class and
registering it, with zero change to the orchestrator, policy engine,
compatibility engine or recovery engine (see `docs/architecture.md` §3.2).

A real deployment would likely run:
- ServiceMesh Core as a hosted service (single-tenant or multi-tenant,
  decision not yet made) that each participating organization connects to via
  an adapter,
- with each organization retaining its own systems and data — ServiceMesh
  does not ask them to migrate anything, only to expose (or have built for
  them) an adapter-compatible API surface.

## Integration strategy

1. Start with **one OEM and its existing authorized service network** as the
   anchor customer — this is the party with the most to gain from visibility
   and coordination, and the party best positioned to require its partners
   (warranty provider, service centres, suppliers) to connect.
2. Build adapters for that OEM's actual marketplace, warranty and supplier
   systems — likely bespoke work per deployment initially, since "every
   organization has a different API" is the premise of this entire project,
   not an exaggeration.
3. Expand to additional OEMs and their networks once the adapter-authoring
   process is well understood from the first deployment.

## Roadmap

**MVP (this repository)**: single vertical (laptops/consumer electronics),
five organization types, the full Service Transaction model, deterministic
recovery, a working demonstration UI, and a research evaluation — all
implemented and tested as documented elsewhere in `docs/`.

**Version 1** (not built): 
- real adapters for at least one production marketplace/warranty/OEM system
  (replacing the simulated organizations for a pilot customer),
- Alembic migrations and a proper schema-evolution story,
- authentication hardening (the current JWT implementation is adequate for a
  demonstration but would need review — token refresh, revocation, rate
  limiting — before production use),
- the provider-metric-snapshot rollup job that currently has a table but no
  scheduled job populating it, which the ML retraining pipeline would need.

**Version 2** (not built):
- expansion beyond laptops to other consumer-electronics categories
  (phones, appliances) within the existing five-organization-type model,
- a proper Temporal (or equivalent) integration behind the existing
  `Workflow.drive()` boundary, if operational experience shows the in-house
  state machine needs the durability guarantees a dedicated engine provides
  at higher transaction volume,
- OpenTelemetry distributed tracing (mentioned throughout as a documented gap
  in this MVP).

**Long-term expansion** (not built, and explicitly speculative): automotive,
industrial equipment, and other complex multi-organization service
ecosystems where the same "no single organization owns the whole customer
request" structure recurs. This is listed as a plausible future direction
because the underlying abstraction (Service Transaction spanning
independently governed parties) is not inherently laptop-specific — but no
work has been done to validate it in any other vertical, and the brief's own
instruction to keep the initial market narrow is followed here: this is
future scope, not a current capability or claim.

## What this document is not

This is not a funded business plan, a market-sizing analysis, or a costed
go-to-market plan. No customer discovery interviews have been conducted for
this project. The personas, competitive categories and roadmap above are
reasoned positioning based on the technical work in this repository, offered
at the level of specificity that work supports — not more.
