"""Provider-risk inference.

Contract with the rest of the system
-----------------------------------
`ProviderRiskScorer` returns a probability in [0, 1] that an operation with
this provider will fail. `engines/selection.py` subtracts a small multiple of
it from the provider's score - and only *after* hard-constraint filtering has
already run. Three consequences, all intentional:

1. The model can reorder eligible providers. It cannot create eligibility.
2. If the model is missing, stale or throws, selection continues unchanged.
   `ml_enabled=false` is a supported production configuration, not a
   degraded one.
3. Its influence is bounded (0.1 of the score range), so a badly calibrated
   model degrades routing quality rather than breaking correctness.

This is the "AI where it provides measurable value" requirement taken
literally: the deterministic core is complete without it, and the ML sits on
top as a ranking signal whose contribution can be measured by turning it off.
"""

from __future__ import annotations

import logging
from pathlib import Path

from servicemesh.core.config import get_settings
from servicemesh.ml.dataset import OPERATION_TYPES, PROVIDER_TYPES
from servicemesh.ml.train import MODEL_VERSION

logger = logging.getLogger("servicemesh.ml")


class ProviderRiskScorer:
    """Loads the serialized model once and scores Provider ORM objects."""

    def __init__(self, model_path: str | None = None) -> None:
        settings = get_settings()
        self.model_path = model_path or settings.ml_model_path
        self._bundle = None
        self._unavailable = False
        self.operation_type: str = "CHECK_PART_AVAILABILITY"
        self.is_urgent: int = 0

    # ----------------------------------------------------------- loading
    @property
    def available(self) -> bool:
        return self._load() is not None

    def _load(self):
        if self._bundle is not None or self._unavailable:
            return self._bundle
        settings = get_settings()
        if not settings.ml_enabled:
            self._unavailable = True
            return None
        path = Path(self.model_path)
        if not path.exists():
            logger.info(
                "provider-risk model not found at %s; selection will run "
                "deterministically (train it with: python -m servicemesh.ml.train)",
                path,
            )
            self._unavailable = True
            return None
        try:
            import joblib

            bundle = joblib.load(path)
            if bundle.get("version") != MODEL_VERSION:
                logger.warning(
                    "model version mismatch (%s != %s); ignoring model",
                    bundle.get("version"), MODEL_VERSION,
                )
                self._unavailable = True
                return None
            self._bundle = bundle
            logger.info("loaded provider-risk model %s", MODEL_VERSION)
            return bundle
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not load provider-risk model: %s", exc)
            self._unavailable = True
            return None

    # ---------------------------------------------------------- features
    def _features(self, provider) -> list[float]:
        """Build the feature vector from live observations of this provider."""
        try:
            provider_idx = PROVIDER_TYPES.index(provider.kind)
        except ValueError:
            provider_idx = 0
        try:
            operation_idx = OPERATION_TYPES.index(self.operation_type)
        except ValueError:
            operation_idx = 0

        return [
            float(provider.success_rate),
            float(provider.avg_latency_ms),
            float(provider.failed_operations),
            float(provider.failed_operations),  # retries proxy
            float(provider.sla_violations),
            float(provider.available_capacity),
            float(provider.cost_index),
            float(provider.distance_km),
            float(provider.sla_hours),
            float(provider.total_operations),
            float(provider_idx),
            float(operation_idx),
            float(self.is_urgent),
        ]

    # ------------------------------------------------------------ score
    def __call__(self, provider) -> float | None:
        bundle = self._load()
        if bundle is None:
            return None
        try:
            import numpy as np

            x = np.array([self._features(provider)], dtype=float)
            return float(bundle["model"].predict_proba(x)[0, 1])
        except Exception as exc:  # noqa: BLE001
            # Never let inference break provider selection.
            logger.warning("provider-risk inference failed: %s", exc)
            return None

    def explain(self, provider) -> dict:
        risk = self(provider)
        return {
            "provider_code": provider.code,
            "model_version": MODEL_VERSION if risk is not None else None,
            "risk_score": None if risk is None else round(risk, 4),
            "available": risk is not None,
            "authority": (
                "advisory only - applied after hard-constraint filtering and "
                "bounded to 0.1 of the selection score range"
            ),
        }


_scorer: ProviderRiskScorer | None = None


def get_scorer() -> ProviderRiskScorer | None:
    """Shared scorer, or None when ML is disabled or unavailable."""
    global _scorer
    if _scorer is None:
        _scorer = ProviderRiskScorer()
    return _scorer if _scorer.available else None


def reset_scorer() -> None:
    global _scorer
    _scorer = None
