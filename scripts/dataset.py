"""Canonical synthetic dataset for ServiceMesh.

SYNTHETIC DATA NOTICE
---------------------
Everything produced here is generated. No customer, order, warranty contract,
inventory level or provider performance figure in this project comes from a
real company. Results computed over this data describe the behaviour of the
implementation under a controlled synthetic workload and must never be
presented as industry measurements.

Why one generator for five databases
------------------------------------
The organizations are independent at runtime, but their data has to agree at
the seams: the serial the marketplace sold must exist in the OEM registry, must
have a warranty contract, and its battery SKU must be carried by at least one
supplier. Generating each organization's slice from one deterministic source
(seeded RNG) guarantees those joins hold without the services ever talking to
each other.

Scenario fixtures
-----------------
Specific serials are pinned to specific outcomes so that the ten demonstration
scenarios are reproducible rather than luck:

  SN-AX14-0001  happy path
  SN-AX14-0002  warranty expired        -> terminal rejection
  SN-AX14-0003  issue type excluded     -> coverage rejection
  SN-AX14-0004  voided contract         -> terminal rejection
  SN-AX15-0001  happy path, Nagpur customer -> unauthorized provider first
  SN-AX15-0002  battery SKU out of stock at nearest supplier -> fallback
  SN-ZN13-0001  happy path, premium plan
  SN-BX14-0001  different OEM (BETA)
  SN-UNKNOWN-1  not in OEM registry     -> product verification fails
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

SEED = 42
NOW = datetime(2026, 9, 15, tzinfo=UTC)

CITIES = {
    "Pune": (18.5204, 73.8567),
    "Mumbai": (19.0760, 72.8777),
    "Nagpur": (21.1458, 79.0882),
    "Bengaluru": (12.9716, 77.5946),
}


def _uid() -> str:
    return str(uuid.uuid4())


@dataclass
class Dataset:
    models: list[dict] = field(default_factory=list)
    components: list[dict] = field(default_factory=list)
    compatibility: list[dict] = field(default_factory=list)
    customers: list[dict] = field(default_factory=list)
    orders: list[dict] = field(default_factory=list)
    units: list[dict] = field(default_factory=list)
    contracts: list[dict] = field(default_factory=list)
    providers: list[dict] = field(default_factory=list)
    oem_partners: list[dict] = field(default_factory=list)
    branches: list[dict] = field(default_factory=list)
    stock: list[dict] = field(default_factory=list)
    users: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Catalogue: models, components, compatibility
# ---------------------------------------------------------------------------

MODEL_SPECS = [
    # model_code, name, manufacturer, year, warranty_months
    ("AX14-PRO-2023", "Acme AX14 Pro 14\"", "ACME", 2023, 24),
    ("AX15-AIR-2022", "Acme AX15 Air 15\"", "ACME", 2022, 12),
    ("ZN13-ULTRA-2024", "Acme ZN13 Ultra 13\"", "ACME", 2024, 24),
    ("BX14-PRO-2023", "Beta BX14 Pro 14\"", "BETA", 2023, 12),
]

COMPONENT_TYPES = ["BATTERY", "SCREEN", "KEYBOARD", "SSD", "FAN"]

#: (sku, name, type, manufacturer, designed_for_model, specs, oem_certified, revision)
COMPONENT_SPECS = [
    ("BAT-AX14-A", "AX14 Battery 56Wh Rev A", "BATTERY", "ACME", "AX14-PRO-2023",
     {"capacity_wh": 56, "voltage": 11.4, "connector": "ACME-B7"}, True, "A"),
    ("BAT-AX14-B", "AX14 Battery 56Wh Rev B", "BATTERY", "ACME", "AX14-PRO-2023",
     {"capacity_wh": 56, "voltage": 11.4, "connector": "ACME-B7", "min_bios": "1.5"},
     True, "B"),
    ("BAT-AX15-A", "AX15 Battery 48Wh", "BATTERY", "ACME", "AX15-AIR-2022",
     {"capacity_wh": 48, "voltage": 11.1, "connector": "ACME-B6"}, True, "A"),
    ("BAT-ZN13-A", "ZN13 Battery 60Wh", "BATTERY", "ACME", "ZN13-ULTRA-2024",
     {"capacity_wh": 60, "voltage": 11.6, "connector": "ACME-B8"}, True, "A"),
    ("BAT-BX14-A", "BX14 Battery 54Wh", "BATTERY", "BETA", "BX14-PRO-2023",
     {"capacity_wh": 54, "voltage": 11.4, "connector": "BETA-K2"}, True, "A"),
    # Third-party generic battery: physically similar, explicitly NOT certified.
    ("BAT-GENERIC-X", "UniCell Generic Battery 55Wh", "BATTERY", "UNICELL", None,
     {"capacity_wh": 55, "voltage": 11.4, "connector": "ACME-B7"}, False, "A"),
    ("SCR-AX14-A", "AX14 14\" FHD Panel", "SCREEN", "ACME", "AX14-PRO-2023",
     {"size_in": 14.0, "resolution": "1920x1080", "connector": "eDP-30"}, True, "A"),
    ("SCR-AX15-A", "AX15 15\" FHD Panel", "SCREEN", "ACME", "AX15-AIR-2022",
     {"size_in": 15.6, "resolution": "1920x1080", "connector": "eDP-30"}, True, "A"),
    ("KBD-AX14-A", "AX14 Keyboard (US)", "KEYBOARD", "ACME", "AX14-PRO-2023",
     {"layout": "US"}, True, "A"),
    ("SSD-NVME-512", "512GB NVMe M.2 2280", "SSD", "ACME", None,
     {"capacity_gb": 512, "interface": "NVMe", "form_factor": "M.2-2280"}, True, "A"),
    ("FAN-AX14-A", "AX14 Cooling Fan", "FAN", "ACME", "AX14-PRO-2023",
     {"rpm_max": 4800}, True, "A"),
]

#: Explicit compatibility matrix. An absent pair is UNKNOWN, not INCOMPATIBLE.
#: (model_code, sku, verdict, constraints, note)
COMPATIBILITY_SPECS = [
    ("AX14-PRO-2023", "BAT-AX14-A", "COMPATIBLE", {}, "OEM original part"),
    ("AX14-PRO-2023", "BAT-AX14-B", "COMPATIBLE", {"min_bios": "1.5"},
     "Rev B requires BIOS 1.5 or newer"),
    ("AX14-PRO-2023", "BAT-AX15-A", "INCOMPATIBLE", {},
     "different connector (ACME-B6) and chassis"),
    ("AX14-PRO-2023", "BAT-GENERIC-X", "INCOMPATIBLE", {},
     "not OEM certified; voids service authorization"),
    ("AX14-PRO-2023", "SCR-AX14-A", "COMPATIBLE", {}, "OEM original part"),
    ("AX14-PRO-2023", "KBD-AX14-A", "COMPATIBLE", {}, "OEM original part"),
    ("AX14-PRO-2023", "SSD-NVME-512", "COMPATIBLE", {}, "standard M.2 2280 slot"),
    ("AX14-PRO-2023", "FAN-AX14-A", "COMPATIBLE", {}, "OEM original part"),
    ("AX15-AIR-2022", "BAT-AX15-A", "COMPATIBLE", {}, "OEM original part"),
    ("AX15-AIR-2022", "BAT-AX14-A", "INCOMPATIBLE", {}, "different chassis"),
    ("AX15-AIR-2022", "SCR-AX15-A", "COMPATIBLE", {}, "OEM original part"),
    ("AX15-AIR-2022", "SSD-NVME-512", "COMPATIBLE", {}, "standard M.2 2280 slot"),
    ("ZN13-ULTRA-2024", "BAT-ZN13-A", "COMPATIBLE", {}, "OEM original part"),
    ("ZN13-ULTRA-2024", "SSD-NVME-512", "COMPATIBLE", {}, "standard M.2 2280 slot"),
    ("BX14-PRO-2023", "BAT-BX14-A", "COMPATIBLE", {}, "OEM original part"),
]


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------

#: code, name, kind, city, sla_hours, cost_index, capacity, authorized_mfrs,
#: oem_authorized (whether the OEM registry lists it)
PROVIDER_SPECS = [
    ("MKT-GLOBAL", "Amazon Marketplace Simulator", "MARKETPLACE", "Mumbai", 24, 1.0, 999,
     ["ACME", "BETA"], True),
    ("OEM-ACME", "Dell OEM Simulator", "MANUFACTURER", "Bengaluru", 24, 1.0, 999,
     ["ACME"], True),
    ("WTY-SHIELD", "Warranty Provider Simulator", "WARRANTY", "Mumbai", 24, 1.0, 999,
     ["ACME", "BETA"], True),

    # Service centres
    ("SVC-PUNE-01", "Pune Authorized Service Centre", "SERVICE_CENTRE", "Pune", 24, 1.0, 8,
     ["ACME", "BETA"], True),
    ("SVC-PUNE-02", "Pune East Service Point", "SERVICE_CENTRE", "Pune", 48, 0.9, 5,
     ["ACME"], True),
    ("SVC-MUM-01", "Mumbai Authorized Service Centre", "SERVICE_CENTRE", "Mumbai", 36, 1.1, 10,
     ["ACME", "BETA"], True),
    ("SVC-NAG-01", "Nagpur Authorized Service Centre", "SERVICE_CENTRE", "Nagpur", 48, 1.2, 6,
     ["ACME"], True),
    # Cheapest and nearest for a Nagpur customer, but NOT in the OEM registry.
    # This is what makes the "unauthorized provider" scenario data-driven
    # rather than hard-coded.
    ("SVC-NAG-GREY", "QuickFix Nagpur (independent)", "SERVICE_CENTRE", "Nagpur", 12, 0.55, 12,
     ["ACME", "BETA"], False),

    # Suppliers
    ("SUP-A", "PartsHub West (primary)", "PARTS_SUPPLIER", "Pune", 24, 1.0, 999,
     ["ACME", "BETA"], True),
    ("SUP-B", "NationalParts (fallback)", "PARTS_SUPPLIER", "Mumbai", 48, 1.15, 999,
     ["ACME", "BETA"], True),
    ("SUP-C", "Central Spares", "PARTS_SUPPLIER", "Nagpur", 72, 0.95, 999,
     ["ACME"], True),
]

#: Stock levels. (supplier_code, sku, on_hand, unit_price, lead_days)
#: BAT-AX15-A is deliberately zero at SUP-A (nearest for a Pune customer) so
#: that the fallback-supplier path is exercised by real data.
STOCK_SPECS = [
    ("SUP-A", "BAT-AX14-A", 12, 4200.0, 1),
    ("SUP-A", "BAT-AX14-B", 6, 4450.0, 1),
    ("SUP-A", "BAT-AX15-A", 0, 3900.0, 3),   # <- forces fallback
    ("SUP-A", "SCR-AX14-A", 4, 9800.0, 2),
    ("SUP-A", "KBD-AX14-A", 9, 2100.0, 1),
    ("SUP-A", "SSD-NVME-512", 20, 3600.0, 1),
    ("SUP-A", "FAN-AX14-A", 7, 1400.0, 1),
    ("SUP-B", "BAT-AX14-A", 5, 4600.0, 2),
    ("SUP-B", "BAT-AX15-A", 8, 4100.0, 2),   # <- fallback has it
    ("SUP-B", "BAT-ZN13-A", 4, 5200.0, 2),
    ("SUP-B", "BAT-BX14-A", 3, 4300.0, 3),
    ("SUP-B", "SCR-AX15-A", 3, 10200.0, 3),
    ("SUP-B", "SSD-NVME-512", 15, 3750.0, 2),
    ("SUP-C", "BAT-AX14-A", 2, 4050.0, 4),
    ("SUP-C", "BAT-ZN13-A", 2, 5100.0, 4),
    ("SUP-C", "SSD-NVME-512", 6, 3550.0, 4),
]


# ---------------------------------------------------------------------------
# Customers / orders / units / contracts
# ---------------------------------------------------------------------------

#: external_ref, name, email, city
CUSTOMER_SPECS = [
    ("CUST-0001", "Aarti Deshpande", "aarti@example.com", "Pune"),
    ("CUST-0002", "Rohit Kulkarni", "rohit@example.com", "Nagpur"),
    ("CUST-0003", "Meera Nair", "meera@example.com", "Mumbai"),
    ("CUST-0004", "Vikram Shah", "vikram@example.com", "Bengaluru"),
]

#: serial, model, customer_ref, order_ref, months_since_purchase, warranty_state,
#: plan, excluded_issue_types
UNIT_SPECS = [
    ("SN-AX14-0001", "AX14-PRO-2023", "CUST-0001", "ORD-100001", 6, "VALID",
     "STANDARD", ["PHYSICAL_DAMAGE", "LIQUID_DAMAGE"]),
    ("SN-AX14-0002", "AX14-PRO-2023", "CUST-0001", "ORD-100002", 40, "EXPIRED",
     "STANDARD", ["PHYSICAL_DAMAGE", "LIQUID_DAMAGE"]),
    ("SN-AX14-0003", "AX14-PRO-2023", "CUST-0003", "ORD-100003", 8, "VALID",
     "BASIC", ["PHYSICAL_DAMAGE", "LIQUID_DAMAGE", "BATTERY_FAILURE"]),
    ("SN-AX14-0004", "AX14-PRO-2023", "CUST-0003", "ORD-100004", 5, "VOID",
     "STANDARD", ["PHYSICAL_DAMAGE"]),
    ("SN-AX15-0001", "AX15-AIR-2022", "CUST-0002", "ORD-100005", 7, "VALID",
     "STANDARD", ["PHYSICAL_DAMAGE", "LIQUID_DAMAGE"]),
    ("SN-AX15-0002", "AX15-AIR-2022", "CUST-0001", "ORD-100006", 4, "VALID",
     "STANDARD", ["PHYSICAL_DAMAGE", "LIQUID_DAMAGE"]),
    ("SN-ZN13-0001", "ZN13-ULTRA-2024", "CUST-0004", "ORD-100007", 3, "VALID",
     "PREMIUM", ["LIQUID_DAMAGE"]),
    ("SN-BX14-0001", "BX14-PRO-2023", "CUST-0004", "ORD-100008", 9, "VALID",
     "STANDARD", ["PHYSICAL_DAMAGE", "LIQUID_DAMAGE"]),
]

PLAN_COVERAGE = {
    "BASIC": ["SCREEN", "KEYBOARD"],
    "STANDARD": ["BATTERY", "SCREEN", "KEYBOARD", "FAN"],
    "PREMIUM": ["BATTERY", "SCREEN", "KEYBOARD", "FAN", "SSD"],
}
PLAN_LIMITS = {"BASIC": 20000.0, "STANDARD": 50000.0, "PREMIUM": 120000.0}
PLAN_DEDUCTIBLE = {"BASIC": 1500.0, "STANDARD": 500.0, "PREMIUM": 0.0}


def build_dataset(extra_units: int = 0, seed: int = SEED) -> Dataset:
    """Build the full cross-organization dataset.

    `extra_units` adds randomised units/orders/contracts on top of the pinned
    scenario fixtures - used by the experiment harness to create a larger
    workload without disturbing the deterministic demo cases.
    """
    rng = random.Random(seed)
    ds = Dataset()

    # --- models -----------------------------------------------------------
    model_ids: dict[str, str] = {}
    for code, name, mfr, year, months in MODEL_SPECS:
        mid = _uid()
        model_ids[code] = mid
        ds.models.append({
            "id": mid, "model_code": code, "name": name, "manufacturer_code": mfr,
            "category": "LAPTOP", "release_year": year,
            "standard_warranty_months": months,
            "attributes": {"form_factor": "clamshell"},
        })

    # --- components -------------------------------------------------------
    comp_ids: dict[str, str] = {}
    for sku, name, ctype, mfr, model_code, specs, certified, rev in COMPONENT_SPECS:
        cid = _uid()
        comp_ids[sku] = cid
        ds.components.append({
            "id": cid, "sku": sku, "name": name, "component_type": ctype,
            "manufacturer_code": mfr,
            "product_model_id": model_ids.get(model_code) if model_code else None,
            "specs": specs, "is_oem_certified": certified, "revision": rev,
        })

    # --- compatibility ----------------------------------------------------
    approved_by_model: dict[str, list[str]] = {c: [] for c in model_ids}
    for model_code, sku, verdict, constraints, note in COMPATIBILITY_SPECS:
        ds.compatibility.append({
            "id": _uid(), "product_model_id": model_ids[model_code],
            "component_id": comp_ids[sku], "verdict": verdict,
            "constraints": constraints, "rule_source": "OEM_MATRIX", "notes": note,
        })
        if verdict == "COMPATIBLE":
            approved_by_model[model_code].append(sku)

    for m in ds.models:
        m["approved_component_skus"] = approved_by_model[m["model_code"]]

    # --- providers --------------------------------------------------------
    for code, name, kind, city, sla, cost, cap, mfrs, oem_ok in PROVIDER_SPECS:
        lat, lon = CITIES[city]
        ds.providers.append({
            "id": _uid(), "code": code, "name": name, "kind": kind, "city": city,
            "region": "IN", "latitude": lat, "longitude": lon,
            "sla_hours": sla, "cost_index": cost, "capacity_total": cap,
            "authorized_manufacturer_codes": mfrs,
            "oem_authorized": oem_ok,
        })
        if kind == "SERVICE_CENTRE":
            ds.branches.append({
                "id": _uid(), "partner_code": code, "name": name, "city": city,
                "region": "IN", "daily_capacity": cap, "booked_today": 0,
                "is_open": True,
            })
            if oem_ok:
                ds.oem_partners.append({
                    "id": _uid(), "partner_code": code, "partner_name": name,
                    "region": "IN", "authorization_level": "FULL", "is_active": True,
                    "authorized_categories": ["LAPTOP"],
                })

    # --- customers --------------------------------------------------------
    cust_ids: dict[str, str] = {}
    for ref, name, email, city in CUSTOMER_SPECS:
        cid = _uid()
        cust_ids[ref] = cid
        lat, lon = CITIES[city]
        ds.customers.append({
            "id": cid, "external_ref": ref, "full_name": name, "email": email,
            "phone": f"+91-9{rng.randint(100000000, 999999999)}", "city": city,
            "region": "IN", "latitude": lat, "longitude": lon,
        })

    # --- units / orders / contracts --------------------------------------
    def add_unit(serial, model_code, cust_ref, order_ref, months_ago,
                 wty_state, plan, excluded):
        model = next(m for m in ds.models if m["model_code"] == model_code)
        purchased = NOW - timedelta(days=int(months_ago * 30.4))
        cust = next(c for c in ds.customers if c["external_ref"] == cust_ref)

        ds.units.append({
            "id": _uid(), "serial_number": serial, "model_code": model_code,
            "manufactured_on": purchased - timedelta(days=rng.randint(20, 90)),
            "plant_code": rng.choice(["PLANT-A", "PLANT-B"]),
            "is_recalled": False,
            "bios_version": rng.choice(["1.4", "1.5", "1.6", "1.7"]),
        })

        price = {"AX14-PRO-2023": 84999.0, "AX15-AIR-2022": 65999.0,
                 "ZN13-ULTRA-2024": 119999.0, "BX14-PRO-2023": 72999.0}[model_code]
        ds.orders.append({
            "id": _uid(), "order_reference": order_ref,
            "customer_external_ref": cust_ref, "customer_id": cust["id"],
            "purchased_at": purchased, "status": "DELIVERED", "channel": "ONLINE",
            "total_amount": price, "currency": "INR",
            "items": [{
                "id": _uid(), "model_code": model_code,
                "product_name": model["name"], "serial_number": serial,
                "seller_code": "SELLER-001", "unit_price": price,
            }],
        })

        # Warranty contract, unless the fixture says there is none
        if wty_state == "NONE":
            return
        starts = purchased
        months = model["standard_warranty_months"]
        expires = starts + timedelta(days=int(months * 30.4))
        if wty_state == "EXPIRED":
            expires = NOW - timedelta(days=45)
        ds.contracts.append({
            # Full serial, not a suffix: SN-AX14-0001 and SN-BX14-0001 share
            # their last 8 characters and would otherwise collide.
            "id": _uid(), "contract_no": f"WC-{serial}",
            "serial_number": serial, "model_code": model_code, "plan_code": plan,
            "starts_on": starts, "expires_on": expires,
            "is_void": wty_state == "VOID",
            "void_reason": ("unauthorized third-party repair detected"
                            if wty_state == "VOID" else None),
            "region": "IN",
            "covered_component_types": PLAN_COVERAGE[plan],
            "excluded_issue_types": excluded,
            "coverage_limit": PLAN_LIMITS[plan],
            "deductible": PLAN_DEDUCTIBLE[plan],
            "claims_used": 0, "max_claims": 3,
        })

    for spec in UNIT_SPECS:
        add_unit(*spec)

    # --- randomised bulk units for experiments ---------------------------
    for i in range(extra_units):
        model_code = rng.choice([m[0] for m in MODEL_SPECS])
        cust_ref = rng.choice([c[0] for c in CUSTOMER_SPECS])
        state = rng.choices(["VALID", "EXPIRED", "VOID"], weights=[0.8, 0.15, 0.05])[0]
        plan = rng.choices(["BASIC", "STANDARD", "PREMIUM"], weights=[0.2, 0.6, 0.2])[0]
        add_unit(
            f"SN-GEN-{i:05d}", model_code, cust_ref, f"ORD-9{i:05d}",
            rng.randint(1, 30) if state == "VALID" else rng.randint(30, 48),
            state, plan, ["PHYSICAL_DAMAGE", "LIQUID_DAMAGE"],
        )

    # --- stock ------------------------------------------------------------
    for supplier_code, sku, on_hand, price, lead in STOCK_SPECS:
        desc = next(c["name"] for c in ds.components if c["sku"] == sku)
        ds.stock.append({
            "id": _uid(), "supplier_code": supplier_code, "sku": sku,
            "description": desc, "on_hand": on_hand, "reserved": 0,
            "unit_price": price, "lead_time_days": lead, "warehouse_region": "IN",
        })

    # --- users (ServiceMesh accounts) ------------------------------------
    ds.users.append({
        "id": _uid(), "email": "admin@servicemesh.io", "password": "admin123",
        "full_name": "Operations Admin", "role": "ADMIN",
    })
    for c in ds.customers:
        ds.users.append({
            "id": _uid(), "email": c["email"], "password": "customer123",
            "full_name": c["full_name"], "role": "CUSTOMER",
            "customer_external_ref": c["external_ref"],
        })
    for p in ds.providers:
        if p["kind"] in {"SERVICE_CENTRE", "PARTS_SUPPLIER"}:
            ds.users.append({
                "id": _uid(),
                "email": f"{p['code'].lower()}@partners.servicemesh.io",
                "password": "provider123",
                "full_name": f"{p['name']} Operator",
                "role": "PROVIDER", "provider_code": p["code"],
            })
    pune = next(
        p for p in ds.providers
        if p["kind"] == "SERVICE_CENTRE" and p["code"] == "SVC-PUNE-01"
    )
    ds.users.append({
        "id": _uid(),
        "email": "repair-person@svc-pune.servicemesh.io",
        "password": "repair123",
        "full_name": "Pune Repair Person",
        "role": "REPAIR_PERSON",
        "provider_code": pune["code"],
    })

    return ds


if __name__ == "__main__":  # pragma: no cover
    d = build_dataset()
    print("models       ", len(d.models))
    print("components   ", len(d.components))
    print("compatibility", len(d.compatibility))
    print("customers    ", len(d.customers))
    print("orders       ", len(d.orders))
    print("units        ", len(d.units))
    print("contracts    ", len(d.contracts))
    print("providers    ", len(d.providers))
    print("oem_partners ", len(d.oem_partners))
    print("branches     ", len(d.branches))
    print("stock        ", len(d.stock))
    print("users        ", len(d.users))
