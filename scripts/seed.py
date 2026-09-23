"""Seed every database in the system from the canonical synthetic dataset.

Run:  python -m scripts.seed --reset
      python -m scripts.seed --reset --extra-units 200   (larger workload)

Writes to six independent databases:
  * ServiceMesh core   (customers, catalogue, compatibility, provider registry,
                        user accounts)
  * marketplace        (orders, order items, buyers)
  * manufacturer       (models, units/serials, authorized partners)
  * warranty           (contracts, coverage terms)
  * service_centre     (branches and their capacity)
  * parts_supplier     (stock levels)

Idempotent by construction: `--reset` drops and recreates. Without `--reset` it
skips rows whose natural key already exists, so re-running is safe.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC

from sqlalchemy import select

from scripts.dataset import Dataset, build_dataset
from servicemesh.core.config import get_settings
from servicemesh.core.db import Base, get_engine, session_scope
from servicemesh.core.models import (
    CompatibilityRule,
    Component,
    Customer,
    ProductModel,
    Provider,
    User,
)
from servicemesh.core.security import hash_password


def _aware(dt):
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


# ---------------------------------------------------------------------------
# ServiceMesh core
# ---------------------------------------------------------------------------


def seed_core(ds: Dataset, reset: bool) -> None:
    engine = get_engine()
    if reset:
        Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    settings = get_settings()
    with session_scope() as db:
        existing = db.scalar(select(ProductModel).limit(1))
        if existing is not None and not reset:
            print("  core: already seeded, skipping")
            return

        for m in ds.models:
            db.add(ProductModel(
                id=m["id"], model_code=m["model_code"], name=m["name"],
                manufacturer_code=m["manufacturer_code"], category=m["category"],
                release_year=m["release_year"], attributes=m["attributes"],
            ))
        db.flush()  # parents must exist before children (FKs are enforced)
        for c in ds.components:
            db.add(Component(
                id=c["id"], sku=c["sku"], name=c["name"],
                component_type=c["component_type"],
                manufacturer_code=c["manufacturer_code"],
                product_model_id=c["product_model_id"], specs=c["specs"],
                is_oem_certified=c["is_oem_certified"], revision=c["revision"],
            ))
        db.flush()
        for r in ds.compatibility:
            db.add(CompatibilityRule(
                id=r["id"], product_model_id=r["product_model_id"],
                component_id=r["component_id"], verdict=r["verdict"],
                constraints=r["constraints"], rule_source=r["rule_source"],
                notes=r["notes"],
            ))
        for cu in ds.customers:
            db.add(Customer(
                id=cu["id"], external_ref=cu["external_ref"], full_name=cu["full_name"],
                email=cu["email"], phone=cu["phone"], region=cu["region"],
                city=cu["city"], latitude=cu["latitude"], longitude=cu["longitude"],
            ))

        url_for_kind = {
            "MARKETPLACE": settings.marketplace_url,
            "MANUFACTURER": settings.manufacturer_url,
            "WARRANTY": settings.warranty_url,
            "SERVICE_CENTRE": settings.service_centre_url,
            "PARTS_SUPPLIER": settings.parts_supplier_url,
        }
        adapter_for_kind = {
            "MARKETPLACE": "marketplace_v1",
            "MANUFACTURER": "manufacturer_v2",
            "WARRANTY": "warranty_soap_v1",
            "SERVICE_CENTRE": "service_centre_v3",
            "PARTS_SUPPLIER": "parts_supplier_v3",
        }
        for p in ds.providers:
            db.add(Provider(
                id=p["id"], code=p["code"], name=p["name"], kind=p["kind"],
                base_url=url_for_kind[p["kind"]],
                adapter_key=adapter_for_kind[p["kind"]],
                region=p["region"],
                authorized_manufacturer_codes=p["authorized_manufacturer_codes"],
                capacity_total=p["capacity_total"], capacity_used=0,
                sla_hours=p["sla_hours"], cost_index=p["cost_index"],
                latitude=p["latitude"], longitude=p["longitude"],
                metadata_json={"city": p["city"], "oem_authorized": p["oem_authorized"]},
            ))
        db.flush()

        cust_by_ref = {c["external_ref"]: c["id"] for c in ds.customers}
        prov_by_code = {p["code"]: p["id"] for p in ds.providers}
        for u in ds.users:
            db.add(User(
                id=u["id"], email=u["email"],
                hashed_password=hash_password(u["password"]),
                full_name=u["full_name"], role=u["role"],
                customer_id=cust_by_ref.get(u.get("customer_external_ref", "")),
                provider_id=prov_by_code.get(u.get("provider_code", "")),
            ))
    print(f"  core: {len(ds.models)} models, {len(ds.components)} components, "
          f"{len(ds.compatibility)} compatibility rules, {len(ds.providers)} providers, "
          f"{len(ds.users)} users")


# ---------------------------------------------------------------------------
# Marketplace
# ---------------------------------------------------------------------------


def seed_marketplace(ds: Dataset, reset: bool) -> None:
    from providers.marketplace.app import (
        MarketplaceCustomer,
        MarketplaceOrder,
        MarketplaceOrderItem,
        store,
    )

    if reset:
        store.drop_all()
    store.create_all()
    with store.session() as db:
        if db.scalar(select(MarketplaceOrder).limit(1)) is not None and not reset:
            print("  marketplace: already seeded, skipping")
            return
        for c in ds.customers:
            db.add(MarketplaceCustomer(
                id=c["id"], email=c["email"], full_name=c["full_name"],
                city=c["city"], region=c["region"],
            ))
        for o in ds.orders:
            db.add(MarketplaceOrder(
                id=o["id"], order_reference=o["order_reference"],
                customer_id=o["customer_id"], purchased_at=_aware(o["purchased_at"]),
                status=o["status"], channel=o["channel"],
                total_amount=o["total_amount"], currency=o["currency"],
            ))
            for it in o["items"]:
                db.add(MarketplaceOrderItem(
                    id=it["id"], order_id=o["id"], model_code=it["model_code"],
                    product_name=it["product_name"], serial_number=it["serial_number"],
                    seller_code=it["seller_code"], unit_price=it["unit_price"],
                ))
    print(f"  marketplace: {len(ds.orders)} orders")


# ---------------------------------------------------------------------------
# Manufacturer
# ---------------------------------------------------------------------------


def seed_manufacturer(ds: Dataset, reset: bool) -> None:
    from providers.manufacturer.app import (
        OemAuthorizedPartner,
        OemModel,
        OemUnit,
        store,
    )

    if reset:
        store.drop_all()
    store.create_all()
    with store.session() as db:
        if db.scalar(select(OemUnit).limit(1)) is not None and not reset:
            print("  manufacturer: already seeded, skipping")
            return
        model_ids: dict[str, str] = {}
        for m in ds.models:
            model_ids[m["model_code"]] = m["id"]
            db.add(OemModel(
                id=m["id"], model_code=m["model_code"], model_name=m["name"],
                manufacturer_code=m["manufacturer_code"], category=m["category"],
                release_year=m["release_year"],
                standard_warranty_months=m["standard_warranty_months"],
                approved_component_skus=m["approved_component_skus"],
            ))
        for u in ds.units:
            db.add(OemUnit(
                id=u["id"], serial_number=u["serial_number"],
                model_id=model_ids[u["model_code"]],
                manufactured_on=_aware(u["manufactured_on"]),
                plant_code=u["plant_code"], is_recalled=u["is_recalled"],
                bios_version=u["bios_version"],
            ))
        for p in ds.oem_partners:
            db.add(OemAuthorizedPartner(
                id=p["id"], partner_code=p["partner_code"],
                partner_name=p["partner_name"], region=p["region"],
                authorization_level=p["authorization_level"],
                is_active=p["is_active"],
                authorized_categories=p["authorized_categories"],
            ))
    print(f"  manufacturer: {len(ds.units)} units, {len(ds.oem_partners)} authorized partners")


# ---------------------------------------------------------------------------
# Warranty
# ---------------------------------------------------------------------------


def seed_warranty(ds: Dataset, reset: bool) -> None:
    from providers.warranty.app import WarrantyContract, store

    if reset:
        store.drop_all()
    store.create_all()
    with store.session() as db:
        if db.scalar(select(WarrantyContract).limit(1)) is not None and not reset:
            print("  warranty: already seeded, skipping")
            return
        for c in ds.contracts:
            db.add(WarrantyContract(
                id=c["id"], contract_no=c["contract_no"],
                serial_number=c["serial_number"], model_code=c["model_code"],
                plan_code=c["plan_code"], starts_on=_aware(c["starts_on"]),
                expires_on=_aware(c["expires_on"]), is_void=c["is_void"],
                void_reason=c["void_reason"], region=c["region"],
                covered_component_types=c["covered_component_types"],
                excluded_issue_types=c["excluded_issue_types"],
                coverage_limit=c["coverage_limit"], deductible=c["deductible"],
                claims_used=c["claims_used"], max_claims=c["max_claims"],
            ))
    print(f"  warranty: {len(ds.contracts)} contracts")


# ---------------------------------------------------------------------------
# Service centre
# ---------------------------------------------------------------------------


def seed_service_centre(ds: Dataset, reset: bool) -> None:
    from providers.service_centre.app import ServiceCentreBranch, store

    if reset:
        store.drop_all()
    store.create_all()
    with store.session() as db:
        if db.scalar(select(ServiceCentreBranch).limit(1)) is not None and not reset:
            print("  service_centre: already seeded, skipping")
            return
        for b in ds.branches:
            db.add(ServiceCentreBranch(
                id=b["id"], partner_code=b["partner_code"], name=b["name"],
                city=b["city"], region=b["region"],
                daily_capacity=b["daily_capacity"], booked_today=b["booked_today"],
                is_open=b["is_open"],
            ))
    print(f"  service_centre: {len(ds.branches)} branches")


# ---------------------------------------------------------------------------
# Parts supplier
# ---------------------------------------------------------------------------


def seed_parts_supplier(ds: Dataset, reset: bool) -> None:
    from providers.parts_supplier.app import SupplierStock, store

    if reset:
        store.drop_all()
    store.create_all()
    with store.session() as db:
        if db.scalar(select(SupplierStock).limit(1)) is not None and not reset:
            print("  parts_supplier: already seeded, skipping")
            return
        for s in ds.stock:
            db.add(SupplierStock(
                id=s["id"], supplier_code=s["supplier_code"], sku=s["sku"],
                description=s["description"], on_hand=s["on_hand"],
                reserved=s["reserved"], unit_price=s["unit_price"],
                lead_time_days=s["lead_time_days"],
                warehouse_region=s["warehouse_region"],
            ))
    print(f"  parts_supplier: {len(ds.stock)} stock rows")


def seed_all(reset: bool = False, extra_units: int = 0, seed: int = 42) -> Dataset:
    ds = build_dataset(extra_units=extra_units, seed=seed)
    print("Seeding ServiceMesh (synthetic data)...")
    seed_core(ds, reset)
    seed_marketplace(ds, reset)
    seed_manufacturer(ds, reset)
    seed_warranty(ds, reset)
    seed_service_centre(ds, reset)
    seed_parts_supplier(ds, reset)
    print("Done.")
    return ds


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed all ServiceMesh databases")
    parser.add_argument("--reset", action="store_true", help="drop and recreate all tables")
    parser.add_argument("--extra-units", type=int, default=0,
                        help="additional randomised units for experiments")
    parser.add_argument("--seed", type=int, default=42, help="RNG seed")
    args = parser.parse_args(argv)
    seed_all(reset=args.reset, extra_units=args.extra_units, seed=args.seed)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
