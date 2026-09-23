"""Natural-language issue extraction.

What this module is allowed to do
---------------------------------
Turn "my laptop battery is swelling and it shuts down after twenty minutes"
into a *suggestion*: issue_type=BATTERY_SWELLING, urgency=HIGH, plus any order
reference or serial number it can spot in the text.

What it is not allowed to do
----------------------------
Decide anything. Warranty validity, coverage, component compatibility, provider
authorization and money all remain with the deterministic engines, evaluated
against the organizations' own records. The extractor's output is written to
`nlp_extraction` and then re-validated; if it says BATTERY_SWELLING and the
warranty plan excludes battery claims, the transaction is still rejected.

Availability
------------
The rule-based extractor is the default and always runs. An LLM backend can be
enabled with GENAI_ENABLED, and if the API call fails or is slow the rule-based
result is used instead. The system never becomes unavailable because an
external model is unavailable.

Confidence
----------
Low confidence is surfaced, not hidden. Below `REVIEW_THRESHOLD` the API asks
the customer to pick the issue type rather than guessing - a wrong issue type
would route the transaction to the wrong component and waste a real repair
slot.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from servicemesh.core.config import get_settings

logger = logging.getLogger("servicemesh.genai")

REVIEW_THRESHOLD = 0.45

#: issue_type -> (keyword patterns, base confidence contribution)
ISSUE_PATTERNS: dict[str, list[str]] = {
    "BATTERY_SWELLING": [
        r"\bswell(ing|ed|s)?\b", r"\bbulg(e|ing|ed)\b", r"\bpuff(y|ed|ing)\b",
        r"\bexpand(ed|ing)\s+batter",
    ],
    "BATTERY_DRAIN": [
        r"\bbattery\b.*\b(drain|dies|dying|discharg)", r"\bshuts?\s+down\b.*\bminutes?\b",
        r"\bwon'?t\s+hold\s+(a\s+)?charge\b", r"\bbattery\s+life\b",
    ],
    "BATTERY_FAILURE": [
        r"\bbattery\b.*\b(fail|dead|not\s+working|faulty|problem|issue)",
        r"\bnot\s+charging\b", r"\bwon'?t\s+charge\b",
    ],
    "NO_POWER": [
        r"\b(won'?t|will\s+not|does\s+not|doesn'?t)\s+(turn\s+on|power\s+on|boot)\b",
        r"\bno\s+power\b", r"\bcompletely\s+dead\b",
    ],
    "SCREEN_DAMAGE": [
        r"\b(crack|shatter|broke)(ed|en)?\b.*\b(screen|display)\b",
        r"\b(screen|display)\b.*\b(crack|shatter|broke)",
    ],
    "SCREEN_FLICKER": [
        r"\bflicker(ing|s)?\b", r"\b(screen|display)\b.*\b(blink|flash)",
    ],
    "DISPLAY_FAILURE": [
        r"\b(black|blank|no)\s+(screen|display)\b",
        r"\b(screen|display)\b.*\b(not\s+working|fail|dead)",
    ],
    "KEYBOARD_FAILURE": [
        r"\bkeyboard\b.*\b(not\s+working|fail|dead|broken|stuck)",
        r"\bkeys?\b.*\b(not\s+working|stuck|unresponsive)",
    ],
    "STORAGE_FAILURE": [
        r"\b(ssd|hard\s*drive|storage|disk)\b.*\b(fail|error|corrupt|not\s+detected)",
        r"\bdisk\s+error\b",
    ],
    "OVERHEATING": [
        r"\boverheat(ing|s|ed)?\b", r"\b(very|too|extremely)\s+hot\b",
        r"\bburning\s+hot\b",
    ],
    "FAN_NOISE": [
        r"\bfan\b.*\b(nois|loud|rattl|grind)", r"\bloud\s+fan\b",
    ],
}

URGENT_PATTERNS = [
    r"\bswell(ing|ed)?\b", r"\bbulg", r"\bsmok(e|ing)\b", r"\bburn(ing|t)?\b",
    r"\bfire\b", r"\bhot\s+to\s+touch\b", r"\bsparks?\b",
]
HIGH_PATTERNS = [
    r"\burgent\b", r"\bimmediately\b", r"\basap\b", r"\bcan'?t\s+work\b",
    r"\bcompletely\s+(dead|unusable)\b",
]

ORDER_PATTERN = re.compile(r"\b(ORD[-_]?\d{4,})\b", re.IGNORECASE)
SERIAL_PATTERN = re.compile(r"\b(SN[-_][A-Z0-9]{2,}[-_]?\d{3,})\b", re.IGNORECASE)
SKU_PATTERN = re.compile(r"\b((?:BAT|SCR|KBD|SSD|FAN)-[A-Z0-9-]{3,})\b", re.IGNORECASE)


@dataclass
class Extraction:
    issue_type: str
    confidence: float
    urgency: str = "NORMAL"
    symptoms: list[str] = field(default_factory=list)
    product_category: str = "LAPTOP"
    order_ref: str | None = None
    serial_number: str | None = None
    requested_component_sku: str | None = None
    method: str = "rules"
    matched_patterns: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def requires_human_review(self) -> bool:
        return self.confidence < REVIEW_THRESHOLD or self.issue_type == "UNKNOWN"

    def to_dict(self) -> dict:
        return {
            "issue_type": self.issue_type,
            "confidence": round(self.confidence, 3),
            "urgency": self.urgency,
            "symptoms": self.symptoms,
            "product_category": self.product_category,
            "order_ref": self.order_ref,
            "serial_number": self.serial_number,
            "requested_component_sku": self.requested_component_sku,
            "method": self.method,
            "matched_patterns": self.matched_patterns,
            "requires_human_review": self.requires_human_review,
            "notes": self.notes,
            "authority": (
                "advisory only; warranty, coverage, compatibility and provider "
                "authorization are decided by the deterministic engines"
            ),
        }


def extract_rule_based(text: str) -> Extraction:
    lowered = text.lower()

    scores: dict[str, float] = {}
    matched: dict[str, list[str]] = {}
    for issue, patterns in ISSUE_PATTERNS.items():
        hits = [p for p in patterns if re.search(p, lowered)]
        if hits:
            # More distinct pattern hits => more confidence, with diminishing
            # returns so a single strong signal is not drowned out.
            scores[issue] = min(0.95, 0.45 + 0.2 * (len(hits) - 1) + 0.15)
            matched[issue] = hits

    if not scores:
        return Extraction(
            issue_type="UNKNOWN", confidence=0.0, method="rules",
            notes=["no known symptom pattern matched the description"],
            order_ref=_first(ORDER_PATTERN, text),
            serial_number=_first(SERIAL_PATTERN, text),
        )

    issue = max(scores, key=lambda k: (scores[k], k))
    confidence = scores[issue]

    # Ambiguity penalty: several unrelated issue families matching at once
    # means the description probably covers more than one problem.
    families = {_family(k) for k in scores}
    if len(families) > 1:
        confidence *= 0.7

    urgency = "NORMAL"
    if any(re.search(p, lowered) for p in URGENT_PATTERNS):
        urgency = "URGENT"
    elif any(re.search(p, lowered) for p in HIGH_PATTERNS):
        urgency = "HIGH"

    return Extraction(
        issue_type=issue,
        confidence=confidence,
        urgency=urgency,
        symptoms=sorted(scores),
        order_ref=_first(ORDER_PATTERN, text),
        serial_number=_first(SERIAL_PATTERN, text),
        requested_component_sku=_first(SKU_PATTERN, text),
        method="rules",
        matched_patterns=matched.get(issue, []),
        notes=(
            ["multiple symptom families detected; confidence reduced"]
            if len(families) > 1 else []
        ),
    )


def _family(issue: str) -> str:
    for prefix in ("BATTERY", "SCREEN", "DISPLAY", "KEYBOARD", "STORAGE", "FAN", "NO"):
        if issue.startswith(prefix):
            return "DISPLAY" if prefix in {"SCREEN", "DISPLAY"} else prefix
    return issue


def _first(pattern: re.Pattern, text: str) -> str | None:
    m = pattern.search(text)
    return m.group(1).upper() if m else None


def extract_with_llm(text: str) -> Extraction | None:
    """Optional LLM backend. Returns None if unavailable or unusable."""
    settings = get_settings()
    if not settings.genai_enabled or not settings.genai_api_key:
        return None
    try:
        import json

        import httpx

        prompt = (
            "Extract structured fields from a consumer-electronics service "
            "request. Respond ONLY with JSON containing keys: issue_type "
            f"(one of {sorted(ISSUE_PATTERNS)} or UNKNOWN), confidence (0-1), "
            "urgency (LOW|NORMAL|HIGH|URGENT), symptoms (array of strings).\n\n"
            f"Request: {text}"
        )
        response = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": settings.genai_api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-6", "max_tokens": 512,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=8.0,
        )
        response.raise_for_status()
        blocks = response.json().get("content", [])
        raw = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        data = json.loads(raw.replace("```json", "").replace("```", "").strip())

        issue = str(data.get("issue_type", "UNKNOWN")).upper()
        if issue not in ISSUE_PATTERNS and issue != "UNKNOWN":
            # The model returned a label outside the accepted vocabulary.
            # Reject rather than inventing a new issue type.
            logger.warning("LLM returned unknown issue_type %s; discarding", issue)
            return None

        return Extraction(
            issue_type=issue,
            confidence=float(data.get("confidence", 0.5)),
            urgency=str(data.get("urgency", "NORMAL")).upper(),
            symptoms=[str(s) for s in data.get("symptoms", [])][:10],
            order_ref=_first(ORDER_PATTERN, text),
            serial_number=_first(SERIAL_PATTERN, text),
            requested_component_sku=_first(SKU_PATTERN, text),
            method="llm",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("LLM extraction unavailable (%s); using rule-based result", exc)
        return None


def extract_issue(text: str) -> Extraction:
    """Extract, preferring the LLM when it is enabled, reachable and agrees.

    When both backends produce an answer the rule-based result is used as a
    cross-check: agreement raises confidence, disagreement lowers it and flags
    the request for human review rather than silently trusting either one.
    """
    rules = extract_rule_based(text)
    llm = extract_with_llm(text)
    if llm is None:
        return rules

    if llm.issue_type == rules.issue_type:
        llm.confidence = min(0.98, max(llm.confidence, rules.confidence) + 0.1)
        llm.method = "llm+rules(agree)"
        llm.notes.append("rule-based extractor agreed")
        return llm

    llm.confidence = min(llm.confidence, rules.confidence) * 0.6
    llm.method = "llm+rules(disagree)"
    llm.notes.append(
        f"rule-based extractor suggested {rules.issue_type}; "
        f"confidence reduced and flagged for review"
    )
    return llm
