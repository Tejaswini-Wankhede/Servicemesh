"""Provider selection - constraint filtering followed by weighted scoring.

Two phases, never merged
------------------------
Phase 1 (hard constraints) removes providers that are *ineligible*: wrong
region, no capacity, not authorized for the manufacturer, SLA beyond the
transaction's ceiling, already tried and failed. These are boolean facts; a
provider that fails one cannot be redeemed by scoring well elsewhere. Merging
them into the score as heavy penalties - a common shortcut - lets a sufficiently
cheap ineligible provider win, which is exactly the bug the two-phase design
prevents.

Phase 2 (scoring) ranks the survivors on reliability, SLA, cost, distance and
capacity headroom. Weights are configurable per call, so an operator can
prioritise speed for an urgent transaction and cost for a routine one.

Every selection persists its full input set, the eliminated providers with
reasons, and the score breakdown of the winner, so "why this provider?" is
always answerable.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from servicemesh.core.models import Provider

SELECTION_VERSION = "selection-v1"

#: Default weights. Must sum to 1.0 for scores to stay in [0, 1].
DEFAULT_WEIGHTS: dict[str, float] = {
    "reliability": 0.35,
    "sla": 0.25,
    "distance": 0.15,
    "cost": 0.15,
    "capacity": 0.10,
}


@dataclass
class ScoredProvider:
    provider_id: str
    code: str
    name: str
    total_score: float
    components: dict[str, float] = field(default_factory=dict)
    raw: dict = field(default_factory=dict)
    #: Optional ML signal, advisory only - never overrides a hard constraint
    ml_risk_score: float | None = None


@dataclass
class EliminatedProvider:
    provider_id: str
    code: str
    reasons: list[str]


@dataclass
class SelectionResult:
    selected: ScoredProvider | None
    ranked: list[ScoredProvider] = field(default_factory=list)
    eliminated: list[EliminatedProvider] = field(default_factory=list)
    weights: dict[str, float] = field(default_factory=dict)
    version: str = SELECTION_VERSION
    reason: str = ""

    @property
    def found(self) -> bool:
        return self.selected is not None


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


class ProviderSelectionEngine:
    def __init__(self, db: Session) -> None:
        self.db = db

    def select(
        self,
        kind: str,
        *,
        customer_region: str,
        manufacturer_code: str | None = None,
        max_sla_hours: int | None = None,
        exclude_provider_ids: list[str] | None = None,
        customer_lat: float | None = None,
        customer_lon: float | None = None,
        require_capacity: bool = True,
        weights: dict[str, float] | None = None,
        ml_scorer=None,
    ) -> SelectionResult:
        weights = {**DEFAULT_WEIGHTS, **(weights or {})}
        excluded = set(exclude_provider_ids or [])

        providers = list(
            self.db.scalars(select(Provider).where(Provider.kind == kind))
        )

        # ---------------- Phase 1: hard constraints ----------------------
        eligible: list[Provider] = []
        eliminated: list[EliminatedProvider] = []

        for p in providers:
            reasons: list[str] = []
            if p.id in excluded:
                reasons.append("already attempted in this transaction and ruled out")
            if not p.is_active:
                reasons.append("provider is inactive")
            if p.region != customer_region:
                reasons.append(
                    f"operates in region {p.region}, customer is in {customer_region}"
                )
            if manufacturer_code and manufacturer_code not in (
                p.authorized_manufacturer_codes or []
            ):
                reasons.append(f"not authorized for manufacturer {manufacturer_code}")
            if require_capacity and p.available_capacity <= 0:
                reasons.append("no available capacity")
            if max_sla_hours is not None and p.sla_hours > max_sla_hours:
                reasons.append(
                    f"SLA {p.sla_hours}h exceeds transaction ceiling {max_sla_hours}h"
                )

            if reasons:
                eliminated.append(EliminatedProvider(p.id, p.code, reasons))
            else:
                eligible.append(p)

        if not eligible:
            return SelectionResult(
                selected=None, ranked=[], eliminated=eliminated, weights=weights,
                reason=(
                    f"no {kind} provider satisfied the hard constraints "
                    f"({len(eliminated)} eliminated)"
                ),
            )

        # ---------------- Phase 2: weighted scoring ----------------------
        distances: dict[str, float] = {}
        for p in eligible:
            if (customer_lat is not None and customer_lon is not None
                    and p.latitude is not None and p.longitude is not None):
                distances[p.id] = haversine_km(
                    customer_lat, customer_lon, p.latitude, p.longitude
                )
            else:
                distances[p.id] = p.distance_km

        max_distance = max(distances.values()) or 1.0
        max_sla = max(p.sla_hours for p in eligible) or 1
        max_cost = max(p.cost_index for p in eligible) or 1.0
        max_capacity = max(p.available_capacity for p in eligible) or 1

        ranked: list[ScoredProvider] = []
        for p in eligible:
            # Each component is normalised so that 1.0 is best.
            reliability = p.success_rate
            sla_score = 1.0 - (p.sla_hours / max_sla) if max_sla else 1.0
            distance_score = 1.0 - (distances[p.id] / max_distance) if max_distance else 1.0
            cost_score = 1.0 - (p.cost_index / max_cost) if max_cost else 1.0
            capacity_score = p.available_capacity / max_capacity if max_capacity else 0.0

            components = {
                "reliability": round(reliability, 4),
                "sla": round(sla_score, 4),
                "distance": round(distance_score, 4),
                "cost": round(cost_score, 4),
                "capacity": round(capacity_score, 4),
            }
            total = sum(weights[k] * components[k] for k in weights)

            scored = ScoredProvider(
                provider_id=p.id, code=p.code, name=p.name,
                total_score=round(total, 4), components=components,
                raw={
                    "success_rate": round(p.success_rate, 4),
                    "avg_latency_ms": round(p.avg_latency_ms, 1),
                    "sla_hours": p.sla_hours,
                    "cost_index": p.cost_index,
                    "distance_km": round(distances[p.id], 1),
                    "available_capacity": p.available_capacity,
                    "total_operations": p.total_operations,
                    "sla_violations": p.sla_violations,
                },
            )

            # ML is advisory. It can reorder eligible providers; it can never
            # make an ineligible provider eligible, because Phase 1 already ran.
            if ml_scorer is not None:
                try:
                    risk = ml_scorer(p)
                    if risk is not None:
                        scored.ml_risk_score = round(float(risk), 4)
                        scored.components["ml_risk_adjustment"] = round(-0.1 * risk, 4)
                        scored.total_score = round(total - 0.1 * risk, 4)
                except Exception:  # noqa: BLE001 - ML must never break selection
                    scored.ml_risk_score = None

            ranked.append(scored)

        ranked.sort(key=lambda s: (-s.total_score, s.code))
        winner = ranked[0]
        return SelectionResult(
            selected=winner, ranked=ranked, eliminated=eliminated, weights=weights,
            reason=(
                f"selected {winner.code} with score {winner.total_score} from "
                f"{len(ranked)} eligible provider(s); {len(eliminated)} eliminated "
                f"by hard constraints"
            ),
        )
