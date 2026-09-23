"""Tests for the ML component.

The central assertion is not that the model is accurate - it is trained on
synthetic data and its accuracy there means little. What must hold is that the
model is *advisory*: it can reorder eligible providers and can never make an
ineligible one eligible, and the system works identically when it is absent.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from servicemesh.core.models import Customer, Provider
from servicemesh.engines.selection import ProviderSelectionEngine
from servicemesh.ml.dataset import FEATURES, generate_rows


def test_synthetic_dataset_has_learnable_signal():
    rows = generate_rows(2000, seed=3)
    assert len(rows) == 2000
    assert all(f in rows[0] for f in FEATURES)

    failure_rate = sum(r["failed"] for r in rows) / len(rows)
    # A genuine minority class - this is what makes accuracy misleading and
    # ROC-AUC the right headline metric.
    assert 0.15 < failure_rate < 0.45, failure_rate

    # Weak providers must fail more often than strong ones, otherwise there is
    # nothing to learn and the model would be fitting noise.
    weak = [r for r in rows if r["historical_success_rate"] < 0.8]
    strong = [r for r in rows if r["historical_success_rate"] > 0.93]
    weak_rate = sum(r["failed"] for r in weak) / max(1, len(weak))
    strong_rate = sum(r["failed"] for r in strong) / max(1, len(strong))
    assert weak_rate > strong_rate + 0.15, (weak_rate, strong_rate)


def test_dataset_is_deterministic_for_a_seed():
    assert generate_rows(200, seed=11) == generate_rows(200, seed=11)


def test_scorer_returns_none_when_ml_disabled(monkeypatch):
    from servicemesh.core.config import get_settings
    from servicemesh.ml.predict import ProviderRiskScorer

    monkeypatch.setattr(get_settings(), "ml_enabled", False)
    scorer = ProviderRiskScorer()
    assert scorer.available is False


def test_scorer_returns_none_when_model_file_absent():
    from servicemesh.ml.predict import ProviderRiskScorer

    scorer = ProviderRiskScorer(model_path="/nonexistent/no-model.joblib")
    assert scorer.available is False


def test_selection_is_unaffected_when_scorer_unavailable(db):
    """Turning ML off must not change deterministic behaviour."""
    customer = db.scalar(select(Customer).where(Customer.external_ref == "CUST-0001"))
    engine = ProviderSelectionEngine(db)
    kwargs = {
        "customer_region": "IN", "manufacturer_code": "ACME",
        "customer_lat": customer.latitude, "customer_lon": customer.longitude,
    }
    without = engine.select("SERVICE_CENTRE", **kwargs)
    with_none = engine.select("SERVICE_CENTRE", ml_scorer=lambda p: None, **kwargs)
    assert without.selected.code == with_none.selected.code
    assert without.selected.total_score == with_none.selected.total_score


def test_ml_cannot_rescue_an_ineligible_provider(db):
    """A scorer claiming zero risk must not resurrect a filtered-out provider.

    This is the property that keeps ML decision-support rather than
    decision-making: hard constraints run in Phase 1, before any score exists.
    """
    customer = db.scalar(select(Customer).where(Customer.external_ref == "CUST-0001"))
    engine = ProviderSelectionEngine(db)

    everyone = list(db.scalars(select(Provider).where(Provider.kind == "SERVICE_CENTRE")))
    first = engine.select(
        "SERVICE_CENTRE", customer_region="IN", manufacturer_code="ACME",
        customer_lat=customer.latitude, customer_lon=customer.longitude,
    )
    excluded_id = first.selected.provider_id

    # The excluded provider is given the most favourable possible ML signal.
    def biased_scorer(p):
        return 0.0 if p.id == excluded_id else 1.0

    second = engine.select(
        "SERVICE_CENTRE", customer_region="IN", manufacturer_code="ACME",
        customer_lat=customer.latitude, customer_lon=customer.longitude,
        exclude_provider_ids=[excluded_id], ml_scorer=biased_scorer,
    )
    assert second.found
    assert second.selected.provider_id != excluded_id
    assert excluded_id not in [r.provider_id for r in second.ranked]
    assert any(e.provider_id == excluded_id for e in second.eliminated)
    assert len(everyone) > 1


def test_ml_can_reorder_eligible_providers(db):
    """Within the eligible set, the signal must actually do something."""
    customer = db.scalar(select(Customer).where(Customer.external_ref == "CUST-0001"))
    engine = ProviderSelectionEngine(db)
    kwargs = {
        "customer_region": "IN", "manufacturer_code": "ACME",
        "customer_lat": customer.latitude, "customer_lon": customer.longitude,
    }
    plain = engine.select("SERVICE_CENTRE", **kwargs)
    assert len(plain.ranked) >= 2

    winner = plain.selected.provider_id
    penalised = engine.select(
        "SERVICE_CENTRE", ml_scorer=lambda p: 1.0 if p.id == winner else 0.0, **kwargs
    )
    top = [r for r in penalised.ranked if r.provider_id == winner][0]
    assert top.ml_risk_score == 1.0
    assert top.total_score < plain.selected.total_score


def test_broken_scorer_does_not_break_selection(db):
    """An exception inside the model must be absorbed, not propagated."""
    customer = db.scalar(select(Customer).where(Customer.external_ref == "CUST-0001"))
    engine = ProviderSelectionEngine(db)

    def exploding(p):
        raise RuntimeError("model blew up")

    result = engine.select(
        "SERVICE_CENTRE", customer_region="IN", manufacturer_code="ACME",
        customer_lat=customer.latitude, customer_lon=customer.longitude,
        ml_scorer=exploding,
    )
    assert result.found
    assert result.selected.ml_risk_score is None


@pytest.mark.slow
def test_trained_model_beats_naive_baselines(tmp_path):
    """Train a small model and require it to beat the trivial baselines."""
    pytest.importorskip("sklearn")
    from servicemesh.ml.train import train

    report = train(
        csv_path=None, rows=2500, seed=5,
        out_path=str(tmp_path / "m.joblib"),
        report_path=str(tmp_path / "r.json"),
    )
    assert report["beats_baselines"] is True
    best = report["candidates"][report["selected_model"]]
    assert best["test_roc_auc"] > 0.65
    # Must beat the majority-class baseline, which has zero recall by design.
    assert best["test_recall"] > 0.3
    assert report["data"]["source"].startswith("SYNTHETIC")
