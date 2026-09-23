# Research / Experimentation

## Research question

The project can compare a simple sequential point-to-point service flow with stateful ServiceMesh orchestration under failures.

## Measures

- completion rate
- recovery success rate
- recovery time
- manual interventions
- duplicate operations
- SLA violations
- average resolution time
- failure-handling latency
- API overhead

`experiments/` generates synthetic workloads. Synthetic results must remain labelled synthetic and must not be presented as industry benchmarks.

## Contribution framing

**FACT:** cross-organization after-sales workflows involve independently controlled systems.

**IMPLEMENTED FEATURE:** ServiceMesh persists a distributed business transaction, performs deterministic policy/compatibility decisions, uses provider adapters, idempotency, retries, fallback and compensation, and exposes the resulting state through portals.

**PROPOSED DESIGN:** the architecture can be extended with Temporal, Neo4j, OPA, OpenTelemetry, Prometheus and Grafana when those technologies solve a demonstrated operational need.

**RESEARCH HYPOTHESIS:** persisted stateful orchestration may reduce repeated work and improve recovery behavior relative to naive sequential integrations under controlled synthetic failures.

**FUTURE VALIDATION:** real enterprise integrations, independent provider data, larger workloads and prior-art/novelty analysis.

No claim of firstness, patentability or universal performance is made by this repository.
