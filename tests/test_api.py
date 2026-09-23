"""API-level tests: authentication, authorization boundaries and payloads."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from servicemesh.api.main import app


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def login(client: TestClient, email: str, password: str) -> dict:
    r = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------- health


def test_health_reports_database_up(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "UP"
    assert body["database"] == "UP"


def test_openapi_schema_generates(client):
    assert client.get("/openapi.json").status_code == 200


# ------------------------------------------------------------------ auth


def test_login_succeeds_for_each_role(client):
    admin = login(client, "admin@servicemesh.io", "admin123")
    assert admin["role"] == "ADMIN"

    customer = login(client, "aarti@example.com", "customer123")
    assert customer["role"] == "CUSTOMER"
    assert customer["customer_id"]

    provider = login(client, "svc-pune-01@partners.servicemesh.io", "provider123")
    assert provider["role"] == "PROVIDER"
    assert provider["provider_code"] == "SVC-PUNE-01"

    repair_person = login(
        client, "repair-person@svc-pune.servicemesh.io", "repair123"
    )
    assert repair_person["role"] == "REPAIR_PERSON"
    assert repair_person["provider_code"] == "SVC-PUNE-01"


def test_login_rejects_wrong_password(client):
    r = client.post(
        "/api/v1/auth/login",
        json={"email": "aarti@example.com", "password": "wrongpassword"},
    )
    assert r.status_code == 401
    assert r.json()["detail"]["error"] == "invalid_credentials"


def test_login_does_not_leak_whether_account_exists(client):
    missing = client.post(
        "/api/v1/auth/login",
        json={"email": "nobody@example.com", "password": "whatever123"},
    )
    wrong = client.post(
        "/api/v1/auth/login",
        json={"email": "aarti@example.com", "password": "whatever123"},
    )
    assert missing.status_code == wrong.status_code == 401
    assert missing.json() == wrong.json()


def test_protected_endpoints_require_a_token(client):
    for path in (
        "/api/v1/transactions", "/api/v1/admin/metrics",
        "/api/v1/providers/me/jobs", "/api/v1/auth/me",
    ):
        assert client.get(path).status_code == 401, path


def test_garbage_token_is_rejected(client):
    r = client.get("/api/v1/transactions", headers=auth("not-a-real-token"))
    assert r.status_code == 401


# ------------------------------------------------------------------ RBAC


def test_customer_cannot_access_admin_endpoints(client):
    token = login(client, "aarti@example.com", "customer123")["access_token"]
    for path in ("/api/v1/admin/metrics", "/api/v1/admin/customers",
                 "/api/v1/providers"):
        r = client.get(path, headers=auth(token))
        assert r.status_code == 403, f"{path} returned {r.status_code}"


def test_provider_cannot_access_admin_metrics(client):
    token = login(
        client, "svc-pune-01@partners.servicemesh.io", "provider123"
    )["access_token"]
    assert client.get("/api/v1/admin/metrics", headers=auth(token)).status_code == 403


def test_admin_can_read_metrics(client):
    token = login(client, "admin@servicemesh.io", "admin123")["access_token"]
    r = client.get("/api/v1/admin/metrics", headers=auth(token))
    assert r.status_code == 200
    body = r.json()
    for key in ("total_transactions", "completion_rate", "recovery_success_rate",
                "duplicate_operations_prevented", "by_state"):
        assert key in body


# -------------------------------------------------- transaction lifecycle


def _create(client, token, **overrides) -> dict:
    payload = {
        "order_ref": "ORD-100001",
        "serial_number": "SN-AX14-0001",
        "issue_type": "BATTERY_FAILURE",
        "issue_description": "Battery swells and shuts down after 20 minutes",
    }
    payload.update(overrides)
    r = client.post("/api/v1/transactions", json=payload, headers=auth(token))
    assert r.status_code == 201, r.text
    return r.json()


def test_customer_creates_and_reads_own_transaction(client):
    token = login(client, "aarti@example.com", "customer123")["access_token"]
    created = _create(client, token)

    assert created["reference"].startswith("SM-")
    assert created["correlation_id"]
    # Without auto_repair the workflow correctly parks at the repair phase.
    assert created["state"] in {"WAITING", "REPAIR_SCHEDULED"}
    assert created["service_booking_ref"]
    assert created["part_reservation_ref"]

    got = client.get(f"/api/v1/transactions/{created['id']}", headers=auth(token))
    assert got.status_code == 200
    assert got.json()["id"] == created["id"]


def test_transaction_detail_payload_is_backed_by_real_data(client):
    token = login(client, "aarti@example.com", "customer123")["access_token"]
    created = _create(client, token)

    assert created["purchase_evidence"]["verified"] is True
    assert created["product_evidence"]["model_code"] == "AX14-PRO-2023"
    assert created["warranty_evidence"]["valid"] is True
    assert created["coverage_evidence"]["covered"] is True

    assert len(created["participants"]) >= 4
    assert created["operations"], "operations must be present"
    assert created["decisions"], "decisions must be present"
    assert created["events"], "events must be present"

    timeline = created["timeline"]
    assert len(timeline) == 14
    assert timeline[0]["status"] == "COMPLETED"
    # Every node carries a status derived from persisted state.
    assert {n["status"] for n in timeline} <= {
        "COMPLETED", "CURRENT", "PENDING", "FAILED", "SKIPPED"
    }

    reserve = [
        o for o in created["operations"] if o["operation_type"] == "RESERVE_PART"
    ]
    assert reserve and reserve[0]["has_idempotency_protection"] is True
    assert reserve[0]["idempotency_key"]


def test_transaction_refinement_payloads_expose_action_notifications_and_audit(client):
    customer_token = login(client, "aarti@example.com", "customer123")["access_token"]
    created = _create(client, customer_token)

    assert created["state_label"]
    assert created["next_action"]
    assert created["audit_timeline"]
    notifications = client.get(
        f"/api/v1/transactions/{created['id']}/notifications",
        headers=auth(customer_token),
    )
    assert notifications.status_code == 200
    assert notifications.json()
    notification = notifications.json()[0]
    marked = client.post(
        f"/api/v1/transactions/{created['id']}/notifications/{notification['id']}/read",
        headers=auth(customer_token),
    )
    assert marked.status_code == 200
    assert marked.json()["is_read"] is True

    supplier_token = login(
        client, "sup-a@partners.servicemesh.io", "provider123"
    )["access_token"]
    supplier_jobs = client.get(
        "/api/v1/providers/me/supplier-jobs", headers=auth(supplier_token)
    )
    assert supplier_jobs.status_code == 200
    assert supplier_jobs.json()

    workload = client.get("/api/v1/providers/me/workload", headers=auth(supplier_token))
    assert workload.status_code == 200
    assert {"capacity_total", "capacity_used", "available_capacity"} <= workload.json().keys()


def test_customer_cannot_read_another_customers_transaction(client):
    aarti = login(client, "aarti@example.com", "customer123")["access_token"]
    created = _create(client, aarti)

    meera = login(client, "meera@example.com", "customer123")["access_token"]
    r = client.get(f"/api/v1/transactions/{created['id']}", headers=auth(meera))
    # 404 rather than 403 so ids cannot be enumerated.
    assert r.status_code == 404

    listed = client.get("/api/v1/transactions", headers=auth(meera))
    assert created["id"] not in [t["id"] for t in listed.json()]


def test_provider_sees_only_its_own_organizations_operations(client):
    customer = login(client, "aarti@example.com", "customer123")["access_token"]
    _create(client, customer)

    svc = login(
        client, "svc-pune-01@partners.servicemesh.io", "provider123"
    )["access_token"]
    sup = login(client, "sup-a@partners.servicemesh.io", "provider123")["access_token"]

    svc_jobs = client.get("/api/v1/providers/me/jobs", headers=auth(svc)).json()
    sup_jobs = client.get("/api/v1/providers/me/jobs", headers=auth(sup)).json()

    svc_ops = {j["operation_type"] for j in svc_jobs}
    sup_ops = {j["operation_type"] for j in sup_jobs}

    assert svc_ops, "service centre should see its bookings"
    assert not sup_jobs, "supplier operations are not service-centre jobs"
    # Neither organization sees the other's operation types.
    assert "RESERVE_PART" not in svc_ops
    assert "BOOK_SERVICE" not in sup_ops


def test_repair_person_can_update_assigned_job_but_not_provider_actions(client):
    customer = login(client, "aarti@example.com", "customer123")["access_token"]
    created = _create(client, customer)
    repair_person = login(
        client, "repair-person@svc-pune.servicemesh.io", "repair123"
    )["access_token"]

    jobs = client.get("/api/v1/providers/me/jobs", headers=auth(repair_person))
    assert jobs.status_code == 200
    assert any(job["transaction_id"] == created["id"] for job in jobs.json())

    updated = client.post(
        f"/api/v1/providers/me/transactions/{created['id']}/repair",
        json={"status": "REPAIRING", "technician": "TECH-DEMO"},
        headers=auth(repair_person),
    )
    assert updated.status_code == 200, updated.text

    decision = client.post(
        f"/api/v1/providers/me/transactions/{created['id']}/service-decision?action=ACCEPT",
        headers=auth(repair_person),
    )
    assert decision.status_code == 403

    dispatch = client.post(
        f"/api/v1/providers/me/transactions/{created['id']}/supplier-dispatch",
        json={"eta": "tomorrow"},
        headers=auth(repair_person),
    )
    assert dispatch.status_code == 403


def test_unauthenticated_cannot_create_transaction(client):
    r = client.post("/api/v1/transactions", json={
        "order_ref": "ORD-100001", "serial_number": "SN-AX14-0001",
        "issue_type": "BATTERY_FAILURE", "issue_description": "broken battery",
    })
    assert r.status_code == 401


def test_request_validation_rejects_bad_payload(client):
    token = login(client, "aarti@example.com", "customer123")["access_token"]
    r = client.post("/api/v1/transactions", json={
        "order_ref": "x", "serial_number": "", "issue_type": "",
        "issue_description": "no",
    }, headers=auth(token))
    assert r.status_code == 422


# -------------------------------------------------------- provider portal


def test_provider_advances_repair_and_transaction_closes(client):
    customer = login(client, "aarti@example.com", "customer123")["access_token"]
    created = _create(client, customer)
    txn_id = created["id"]

    svc = login(
        client, "svc-pune-01@partners.servicemesh.io", "provider123"
    )["access_token"]

    r = client.post(
        f"/api/v1/providers/me/transactions/{txn_id}/repair",
        json={"status": "REPAIRING", "notes": "diagnosis confirms battery fault"},
        headers=auth(svc),
    )
    assert r.status_code == 200, r.text

    r = client.post(
        f"/api/v1/providers/me/transactions/{txn_id}/repair",
        json={"status": "COMPLETED", "notes": "battery replaced and tested"},
        headers=auth(svc),
    )
    assert r.status_code == 200, r.text
    assert r.json()["final_state"] == "CLOSED"

    detail = client.get(
        f"/api/v1/transactions/{txn_id}", headers=auth(customer)
    ).json()
    assert detail["state"] == "CLOSED"
    assert detail["outcome"] == "RESOLVED"
    assert detail["progress_percent"] == 100.0


def test_wrong_provider_cannot_update_another_providers_repair(client):
    customer = login(client, "aarti@example.com", "customer123")["access_token"]
    created = _create(client, customer)

    other = login(
        client, "svc-mum-01@partners.servicemesh.io", "provider123"
    )["access_token"]
    r = client.post(
        f"/api/v1/providers/me/transactions/{created['id']}/repair",
        json={"status": "COMPLETED"}, headers=auth(other),
    )
    # SVC-MUM-01 does not participate at all, so the transaction is invisible.
    assert r.status_code in (403, 404)


# ------------------------------------------------------------ admin tools


def test_recovery_matrix_and_policies_are_exposed(client):
    token = login(client, "admin@servicemesh.io", "admin123")["access_token"]

    matrix = client.get("/api/v1/admin/recovery-matrix", headers=auth(token)).json()
    rule_ids = {r["rule_id"] for r in matrix["rules"]}
    assert "R1_UNKNOWN_OUTCOME_RECONCILE" in rule_ids
    assert "R6_TRANSIENT_RETRY" in rule_ids

    policies = client.get("/api/v1/admin/policies", headers=auth(token)).json()
    assert "WARRANTY_ELIGIBILITY" in policies["rule_sets"]
    assert "PROVIDER_ELIGIBILITY" in policies["rule_sets"]


def test_admin_can_drive_failure_simulation(client):
    token = login(client, "admin@servicemesh.io", "admin123")["access_token"]

    r = client.post("/api/v1/admin/simulation", headers=auth(token), json={
        "service": "parts_supplier", "mode": "TEMPORARILY_UNAVAILABLE",
        "operation": "inventory.check", "count": 2,
    })
    assert r.status_code == 200
    assert r.json()["rules"][0]["mode"] == "TEMPORARILY_UNAVAILABLE"

    customer = login(client, "aarti@example.com", "customer123")["access_token"]
    created = _create(client, customer)

    # The outage was real: it was recorded, and recovery handled it.
    assert created["failures"], "expected the simulated outage to be recorded"
    assert created["recovery_actions"], "expected recovery to have run"
    assert created["state"] in {"WAITING", "REPAIR_SCHEDULED"}

    assert client.post(
        "/api/v1/admin/simulation/reset", headers=auth(token)
    ).status_code == 200


def test_admin_rejects_unknown_simulation_mode(client):
    token = login(client, "admin@servicemesh.io", "admin123")["access_token"]
    r = client.post("/api/v1/admin/simulation", headers=auth(token), json={
        "service": "parts_supplier", "mode": "NOT_A_REAL_MODE",
    })
    assert r.status_code == 400


def test_metrics_reflect_actual_transactions(client):
    admin = login(client, "admin@servicemesh.io", "admin123")["access_token"]
    before = client.get("/api/v1/admin/metrics", headers=auth(admin)).json()

    customer = login(client, "aarti@example.com", "customer123")["access_token"]
    _create(client, customer)

    after = client.get("/api/v1/admin/metrics", headers=auth(admin)).json()
    assert after["total_transactions"] == before["total_transactions"] + 1
    assert sum(after["by_state"].values()) == after["total_transactions"]


# --------------------------------------------------------- natural language


def test_natural_language_request_creates_transaction(client):
    token = login(client, "aarti@example.com", "customer123")["access_token"]
    r = client.post("/api/v1/transactions/natural-language", headers=auth(token), json={
        "text": (
            "My laptop battery is swelling and the laptop shuts down after "
            "twenty minutes. Order ORD-100001, serial SN-AX14-0001."
        ),
    })
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["nlp_extraction"]["issue_type"] == "BATTERY_SWELLING"
    assert body["nlp_extraction"]["urgency"] == "URGENT"
    assert body["order_ref"] == "ORD-100001"
    assert body["serial_number"] == "SN-AX14-0001"
    # Extraction is advisory; the deterministic engines still ran.
    assert body["warranty_evidence"]["valid"] is True


def test_unintelligible_text_is_flagged_not_guessed(client):
    token = login(client, "aarti@example.com", "customer123")["access_token"]
    r = client.post("/api/v1/transactions/natural-language", headers=auth(token), json={
        "text": "hello please help me with the thing it is not good today thanks",
    })
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "extraction_uncertain"


# ------------------------------------------------- simulation control plane


def test_simulation_reports_its_channel(client):
    """A no-op must be impossible to mistake for success.

    In-process the controller talks to the simulator objects directly; over
    HTTP it calls each organization's own /_sim API. The response says which,
    because the original implementation silently mutated local objects that
    the separate provider processes never saw.
    """
    token = login(client, "admin@servicemesh.io", "admin123")["access_token"]

    status = client.get("/api/v1/admin/simulation", headers=auth(token)).json()
    assert status["channel"] == "inproc"
    assert set(status["services"]) == {
        "marketplace", "manufacturer", "warranty", "service_centre", "parts_supplier"
    }
    assert "TIMEOUT_AFTER_COMMIT" in status["available_modes"]

    applied = client.post("/api/v1/admin/simulation", headers=auth(token), json={
        "service": "warranty", "mode": "TIMEOUT", "count": 1,
    }).json()
    assert applied["channel"] == "inproc"
    assert applied["rules"][0]["mode"] == "TIMEOUT"


def test_simulation_rejects_unknown_service(client):
    token = login(client, "admin@servicemesh.io", "admin123")["access_token"]
    r = client.post("/api/v1/admin/simulation", headers=auth(token), json={
        "service": "not_a_service", "mode": "TIMEOUT",
    })
    assert r.status_code == 422  # rejected by the schema pattern


def test_simulation_http_channel_fails_loudly_when_unreachable(monkeypatch):
    """Over HTTP, an unreachable organization must raise, not pretend."""
    from servicemesh.api import simulation
    from servicemesh.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "provider_transport", "http")
    monkeypatch.setattr(settings, "warranty_url", "http://127.0.0.1:1")

    with pytest.raises(simulation.SimulationError) as exc:
        simulation.set_mode("warranty", "TIMEOUT")
    assert "could not reach warranty" in str(exc.value)


def test_supplier_portal_dispatch_is_persisted_and_idempotent_path_is_real(client):
    customer = login(client, "aarti@example.com", "customer123")["access_token"]
    created = _create(client, customer)
    supplier = login(client, "sup-a@partners.servicemesh.io", "provider123")["access_token"]
    r = client.post(
        f"/api/v1/providers/me/transactions/{created['id']}/supplier-dispatch",
        json={"eta": "1 business day"}, headers=auth(supplier)
    )
    assert r.status_code == 200, r.text
    detail = client.get(f"/api/v1/transactions/{created['id']}", headers=auth(customer)).json()
    assert detail["part_delivery_status"] == "DISPATCHED"


def test_service_centre_rejection_selects_fallback_provider(client):
    customer = login(client, "aarti@example.com", "customer123")["access_token"]
    created = _create(client, customer)
    original = created["service_provider_code"]
    provider = login(client, "svc-pune-01@partners.servicemesh.io", "provider123")["access_token"]
    r = client.post(
        f"/api/v1/providers/me/transactions/{created['id']}/service-decision?action=REJECT",
        headers=auth(provider)
    )
    assert r.status_code == 200, r.text
    detail = client.get(f"/api/v1/transactions/{created['id']}", headers=auth(customer)).json()
    assert detail["service_provider_code"] != original
    assert original in detail["excluded_provider_codes"]
