"""Policy engine - federated business rules, versioned and explainable.

Why an internal engine rather than OPA
--------------------------------------
Open Policy Agent was evaluated and deliberately deferred to future scope. The
rules here need to read live evidence gathered from five organizations mid
transaction, and every decision must be persisted with the exact inputs that
produced it. Running that through an external Rego service would add a network
hop and a second failure domain to the critical path while providing no
capability this engine lacks at current rule volume. The `PolicyEngine.evaluate`
signature is intentionally a pure function of (rule_set, context), which is the
same shape OPA expects, so swapping the evaluator later is a contained change.

Every rule carries an id and a version. When a decision is persisted, the rule
id and version go with it, so a decision made six months ago can still be
explained even after the rules change.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

POLICY_VERSION = "policy-v1"


@dataclass
class RuleOutcome:
    rule_id: str
    passed: bool
    reason: str
    #: True when failing this rule ends the transaction rather than redirecting it
    terminal: bool = False
    details: dict = field(default_factory=dict)


@dataclass
class PolicyDecision:
    allowed: bool
    rule_set: str
    version: str
    outcomes: list[RuleOutcome] = field(default_factory=list)
    #: Convenience view of the failures
    violations: list[RuleOutcome] = field(default_factory=list)

    @property
    def terminal(self) -> bool:
        """Should the transaction stop, rather than try an alternative?"""
        return any(v.terminal for v in self.violations)

    @property
    def reasons(self) -> list[str]:
        return [f"{o.rule_id}: {o.reason}" for o in self.outcomes]

    @property
    def violation_reasons(self) -> list[str]:
        return [f"{v.rule_id}: {v.reason}" for v in self.violations]


Rule = Callable[[dict], RuleOutcome]


class PolicyEngine:
    """Evaluates named rule sets against a context dictionary."""

    def __init__(self, version: str = POLICY_VERSION) -> None:
        self.version = version
        self._rule_sets: dict[str, list[Rule]] = {
            "WARRANTY_ELIGIBILITY": [
                _rule_purchase_verified,
                _rule_product_verified,
                _rule_unit_not_recalled,
                _rule_warranty_valid,
            ],
            "COVERAGE": [
                _rule_coverage_granted,
                _rule_region_match,
            ],
            "PROVIDER_ELIGIBILITY": [
                _rule_provider_active,
                _rule_provider_oem_authorized,
                _rule_provider_region,
                _rule_provider_capacity,
                _rule_provider_sla,
            ],
            "COMPONENT_SELECTION": [
                _rule_component_compatible,
                _rule_component_certified_for_warranty,
            ],
        }

    def evaluate(self, rule_set: str, context: dict) -> PolicyDecision:
        rules = self._rule_sets.get(rule_set)
        if rules is None:
            raise KeyError(f"unknown policy rule set: {rule_set}")

        outcomes = [rule(context) for rule in rules]
        violations = [o for o in outcomes if not o.passed]
        return PolicyDecision(
            allowed=not violations, rule_set=rule_set, version=self.version,
            outcomes=outcomes, violations=violations,
        )

    def rule_sets(self) -> list[str]:
        return sorted(self._rule_sets)

    def describe(self) -> dict:
        return {
            "version": self.version,
            "rule_sets": {
                name: [r.__name__.replace("_rule_", "") for r in rules]
                for name, rules in self._rule_sets.items()
            },
        }


# ---------------------------------------------------------------------------
# Warranty eligibility
# ---------------------------------------------------------------------------


def _rule_purchase_verified(ctx: dict) -> RuleOutcome:
    ok = bool((ctx.get("purchase") or {}).get("verified"))
    return RuleOutcome(
        "PURCHASE_VERIFIED", ok,
        "purchase confirmed by marketplace" if ok
        else (ctx.get("purchase") or {}).get("reason", "purchase could not be verified"),
        terminal=not ok,
    )


def _rule_product_verified(ctx: dict) -> RuleOutcome:
    ok = bool((ctx.get("product") or {}).get("verified"))
    return RuleOutcome(
        "PRODUCT_VERIFIED", ok,
        "serial confirmed in OEM registry" if ok
        else (ctx.get("product") or {}).get("reason", "serial not verified"),
        terminal=not ok,
    )


def _rule_unit_not_recalled(ctx: dict) -> RuleOutcome:
    recalled = bool((ctx.get("product") or {}).get("is_recalled"))
    return RuleOutcome(
        "UNIT_NOT_RECALLED", not recalled,
        "unit is not subject to a recall" if not recalled
        else "unit is under active recall; requires recall handling, not standard repair",
        # A recall is not a customer-facing rejection - it needs a human.
        terminal=False,
        details={"is_recalled": recalled},
    )


def _rule_warranty_valid(ctx: dict) -> RuleOutcome:
    wty = ctx.get("warranty") or {}
    ok = bool(wty.get("valid"))
    return RuleOutcome(
        "WARRANTY_VALID", ok,
        f"active warranty contract {wty.get('contract_no')}" if ok
        else wty.get("reason", "no valid warranty contract"),
        terminal=not ok,
        details={"contract_no": wty.get("contract_no"), "expires_on": wty.get("expires_on")},
    )


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------


def _rule_coverage_granted(ctx: dict) -> RuleOutcome:
    cov = ctx.get("coverage") or {}
    ok = bool(cov.get("covered"))
    return RuleOutcome(
        "COVERAGE_GRANTED", ok,
        f"repair approved for {cov.get('approved_amount')} "
        f"(authorization {cov.get('authorization_code')})" if ok
        else cov.get("reason", "repair not covered under this plan"),
        terminal=not ok,
        details={"approved_amount": cov.get("approved_amount")},
    )


def _rule_region_match(ctx: dict) -> RuleOutcome:
    customer_region = ctx.get("customer_region")
    contract_region = (ctx.get("warranty") or {}).get("region")
    if contract_region is None:
        return RuleOutcome("REGION_MATCH", True, "no regional restriction on contract")
    ok = customer_region == contract_region
    return RuleOutcome(
        "REGION_MATCH", ok,
        f"service requested in contract region {contract_region}" if ok
        else f"contract is valid in {contract_region}, request is from {customer_region}",
        terminal=not ok,
    )


# ---------------------------------------------------------------------------
# Provider eligibility
# ---------------------------------------------------------------------------


def _rule_provider_active(ctx: dict) -> RuleOutcome:
    p = ctx.get("provider") or {}
    ok = bool(p.get("is_active", True))
    return RuleOutcome(
        "PROVIDER_ACTIVE", ok,
        "provider is active" if ok else "provider is deactivated in the registry",
    )


def _rule_provider_oem_authorized(ctx: dict) -> RuleOutcome:
    """Authorization is answered by the OEM, not by our own registry.

    The OEM is the only organization entitled to say who may service its
    products under warranty, so this rule reads the live answer we obtained
    from the OEM service rather than a cached flag.
    """
    auth = ctx.get("provider_authorization")
    if auth is None:
        return RuleOutcome(
            "PROVIDER_OEM_AUTHORIZED", False,
            "OEM authorization has not been checked for this provider",
        )
    ok = bool(auth.get("authorized"))
    return RuleOutcome(
        "PROVIDER_OEM_AUTHORIZED", ok,
        "OEM confirms partner authorization" if ok
        else auth.get("reason", "OEM does not authorize this partner"),
        terminal=False,  # pick a different provider, do not end the transaction
        details={"partner_code": auth.get("partner_code")},
    )


def _rule_provider_region(ctx: dict) -> RuleOutcome:
    p = ctx.get("provider") or {}
    required = ctx.get("customer_region")
    ok = p.get("region") == required
    return RuleOutcome(
        "PROVIDER_REGION", ok,
        f"provider operates in {required}" if ok
        else f"provider region {p.get('region')} does not match customer region {required}",
    )


def _rule_provider_capacity(ctx: dict) -> RuleOutcome:
    p = ctx.get("provider") or {}
    available = p.get("available_capacity", 0)
    ok = available > 0
    return RuleOutcome(
        "PROVIDER_CAPACITY", ok,
        f"{available} slots available" if ok else "no remaining capacity",
        details={"available_capacity": available},
    )


def _rule_provider_sla(ctx: dict) -> RuleOutcome:
    p = ctx.get("provider") or {}
    max_sla = ctx.get("max_sla_hours")
    if max_sla is None:
        return RuleOutcome("PROVIDER_SLA", True, "no SLA ceiling specified")
    ok = p.get("sla_hours", 9999) <= max_sla
    return RuleOutcome(
        "PROVIDER_SLA", ok,
        f"provider SLA {p.get('sla_hours')}h within limit {max_sla}h" if ok
        else f"provider SLA {p.get('sla_hours')}h exceeds limit {max_sla}h",
    )


# ---------------------------------------------------------------------------
# Component selection
# ---------------------------------------------------------------------------


def _rule_component_compatible(ctx: dict) -> RuleOutcome:
    comp = ctx.get("compatibility") or {}
    verdict = comp.get("verdict")
    ok = verdict == "COMPATIBLE"
    return RuleOutcome(
        "COMPONENT_COMPATIBLE", ok,
        "component is compatible with this model" if ok
        else f"compatibility verdict is {verdict}: {'; '.join(comp.get('reasons', []))}",
    )


def _rule_component_certified_for_warranty(ctx: dict) -> RuleOutcome:
    """Under warranty, only OEM-certified parts may be fitted.

    Out of warranty this rule would not apply; it is scoped by
    `under_warranty` in the context rather than being unconditional.
    """
    if not ctx.get("under_warranty", True):
        return RuleOutcome(
            "COMPONENT_CERTIFIED", True, "repair is out of warranty; OEM certification not required"
        )
    certified = bool((ctx.get("component") or {}).get("is_oem_certified"))
    return RuleOutcome(
        "COMPONENT_CERTIFIED", certified,
        "component is OEM certified" if certified
        else "non-certified component cannot be fitted under warranty",
    )
