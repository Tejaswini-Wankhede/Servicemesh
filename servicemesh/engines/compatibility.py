"""Compatibility engine - deterministic, explainable, and the final authority.

Three-valued logic, deliberately
--------------------------------
COMPATIBLE / INCOMPATIBLE / UNKNOWN. The third value is not indecision, it is
the correct answer when no rule exists. Collapsing UNKNOWN into COMPATIBLE
would let ServiceMesh fit an unverified part; collapsing it into INCOMPATIBLE
would silently rule out valid parts the matrix has not been updated for. So
UNKNOWN is surfaced, and policy decides what to do with it (currently: do not
auto-select, escalate if it is the only candidate).

Why no LLM here
---------------
An incorrect compatibility decision damages hardware and voids warranties. A
language model cannot offer the guarantee this needs, and its output is not
reproducible across runs. The GenAI module may *suggest* a component type from
free text, but this engine decides, using explicit rows the OEM published.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from servicemesh.core.enums import CompatibilityVerdict
from servicemesh.core.models import CompatibilityRule, Component, ProductModel

RULE_VERSION = "compat-v1"


@dataclass
class CompatibilityCheck:
    verdict: CompatibilityVerdict
    component_id: str | None
    component_sku: str | None
    reasons: list[str] = field(default_factory=list)
    constraints_evaluated: dict = field(default_factory=dict)
    rule_source: str | None = None
    rule_version: str = RULE_VERSION

    @property
    def is_compatible(self) -> bool:
        return self.verdict is CompatibilityVerdict.COMPATIBLE


@dataclass
class CandidateComponent:
    component_id: str
    sku: str
    name: str
    is_oem_certified: bool
    revision: str
    reasons: list[str] = field(default_factory=list)
    rank_score: float = 0.0


def _version_tuple(value: str | None) -> tuple[int, ...]:
    if not value:
        return (0,)
    parts: list[int] = []
    for chunk in str(value).split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts) or (0,)


class CompatibilityEngine:
    """Evaluates part/model compatibility against stored rules."""

    def __init__(self, db: Session) -> None:
        self.db = db

    # ------------------------------------------------------------- single
    def check(
        self,
        product_model_id: str,
        component_id: str,
        context: dict | None = None,
    ) -> CompatibilityCheck:
        """Is this specific component usable on this specific model?"""
        context = context or {}
        component = self.db.get(Component, component_id)
        if component is None:
            return CompatibilityCheck(
                verdict=CompatibilityVerdict.UNKNOWN,
                component_id=component_id, component_sku=None,
                reasons=["component not present in ServiceMesh catalogue"],
            )

        rule = self.db.scalar(
            select(CompatibilityRule).where(
                CompatibilityRule.product_model_id == product_model_id,
                CompatibilityRule.component_id == component_id,
            )
        )

        if rule is None:
            # No published rule. Not a yes, not a no.
            return CompatibilityCheck(
                verdict=CompatibilityVerdict.UNKNOWN,
                component_id=component_id, component_sku=component.sku,
                reasons=[
                    "no compatibility rule published for this model/component pair",
                    "absence of a rule is treated as UNKNOWN, never as compatible",
                ],
            )

        if rule.verdict == CompatibilityVerdict.INCOMPATIBLE.value:
            return CompatibilityCheck(
                verdict=CompatibilityVerdict.INCOMPATIBLE,
                component_id=component_id, component_sku=component.sku,
                reasons=[rule.notes or "explicitly marked incompatible by OEM matrix"],
                rule_source=rule.rule_source,
            )

        # verdict is COMPATIBLE - now every attached constraint must hold.
        reasons: list[str] = [rule.notes or "listed as compatible in OEM matrix"]
        evaluated: dict = {}
        constraints = rule.constraints or {}

        min_bios = constraints.get("min_bios")
        if min_bios:
            actual = context.get("bios_version")
            evaluated["min_bios"] = {"required": min_bios, "actual": actual}
            if actual is None:
                return CompatibilityCheck(
                    verdict=CompatibilityVerdict.UNKNOWN,
                    component_id=component_id, component_sku=component.sku,
                    reasons=[f"requires BIOS >= {min_bios} but unit BIOS is unknown"],
                    constraints_evaluated=evaluated, rule_source=rule.rule_source,
                )
            if _version_tuple(actual) < _version_tuple(min_bios):
                return CompatibilityCheck(
                    verdict=CompatibilityVerdict.INCOMPATIBLE,
                    component_id=component_id, component_sku=component.sku,
                    reasons=[f"unit BIOS {actual} is below required {min_bios}"],
                    constraints_evaluated=evaluated, rule_source=rule.rule_source,
                )
            reasons.append(f"BIOS constraint satisfied ({actual} >= {min_bios})")

        required_region = constraints.get("region")
        if required_region:
            actual_region = context.get("region")
            evaluated["region"] = {"required": required_region, "actual": actual_region}
            if actual_region != required_region:
                return CompatibilityCheck(
                    verdict=CompatibilityVerdict.INCOMPATIBLE,
                    component_id=component_id, component_sku=component.sku,
                    reasons=[f"component restricted to region {required_region}"],
                    constraints_evaluated=evaluated, rule_source=rule.rule_source,
                )

        # Corroboration against what the OEM told us at verification time.
        approved = context.get("approved_component_skus")
        if approved is not None and component.sku not in approved:
            reasons.append(
                "note: component is in the compatibility matrix but was not in the "
                "OEM's approved list for this serial"
            )

        return CompatibilityCheck(
            verdict=CompatibilityVerdict.COMPATIBLE,
            component_id=component_id, component_sku=component.sku,
            reasons=reasons, constraints_evaluated=evaluated,
            rule_source=rule.rule_source,
        )

    # --------------------------------------------------------- candidates
    def find_compatible(
        self,
        product_model_id: str,
        component_type: str,
        context: dict | None = None,
        exclude_component_ids: list[str] | None = None,
    ) -> list[CandidateComponent]:
        """All components of a type that are COMPATIBLE with this model.

        Returned in preference order: OEM-certified first, then later revisions
        (a newer revision usually supersedes an older one), then SKU for a
        stable, reproducible ordering.
        """
        context = context or {}
        excluded = set(exclude_component_ids or [])

        rows = self.db.execute(
            select(CompatibilityRule, Component)
            .join(Component, Component.id == CompatibilityRule.component_id)
            .where(
                CompatibilityRule.product_model_id == product_model_id,
                CompatibilityRule.verdict == CompatibilityVerdict.COMPATIBLE.value,
                Component.component_type == component_type,
            )
        ).all()

        candidates: list[CandidateComponent] = []
        for _rule, component in rows:
            if component.id in excluded:
                continue
            check = self.check(product_model_id, component.id, context)
            if not check.is_compatible:
                continue
            candidates.append(
                CandidateComponent(
                    component_id=component.id, sku=component.sku, name=component.name,
                    is_oem_certified=component.is_oem_certified,
                    revision=component.revision, reasons=check.reasons,
                )
            )

        candidates.sort(
            key=lambda c: (0 if c.is_oem_certified else 1, _rev_key(c.revision), c.sku)
        )
        for i, c in enumerate(candidates):
            c.rank_score = 1.0 - (i / max(1, len(candidates)))
        return candidates

    def required_component_type(self, issue_type: str) -> str | None:
        """Map a customer issue onto the component type it implicates."""
        return ISSUE_TO_COMPONENT.get(issue_type.upper())

    def model_by_code(self, model_code: str) -> ProductModel | None:
        return self.db.scalar(
            select(ProductModel).where(ProductModel.model_code == model_code)
        )


def _rev_key(revision: str) -> tuple:
    """Later revisions sort first (B before A)."""
    return (-ord(revision[0]),) if revision else (0,)


#: Deterministic issue -> component mapping. Kept as explicit data rather than
#: inference so the choice is auditable and testable.
ISSUE_TO_COMPONENT: dict[str, str] = {
    "BATTERY_FAILURE": "BATTERY",
    "BATTERY_SWELLING": "BATTERY",
    "BATTERY_DRAIN": "BATTERY",
    "NO_POWER": "BATTERY",
    "SCREEN_DAMAGE": "SCREEN",
    "SCREEN_FLICKER": "SCREEN",
    "DISPLAY_FAILURE": "SCREEN",
    "KEYBOARD_FAILURE": "KEYBOARD",
    "KEY_NOT_WORKING": "KEYBOARD",
    "STORAGE_FAILURE": "SSD",
    "DISK_ERROR": "SSD",
    "OVERHEATING": "FAN",
    "FAN_NOISE": "FAN",
}
