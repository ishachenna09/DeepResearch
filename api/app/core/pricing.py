"""List-price table used to put a dollar figure on measured token usage.

Why this exists at all when the stack runs on free tiers?
"Free" is a property of today's account, not of the workload. The dashboard
shows the *shadow cost* - what the same runs would bill at list prices - so a
regression in tokens is visible as money even while the invoice is $0.

Why one blended price per model instead of separate input/output prices?
The app records `tokens_used` (a single total from `usage_metadata`), not a
prompt/completion split per call. Inventing a split after the fact would look
more precise than it is. A fixed 70/30 input/output blend is stated once, here,
and every number derived from it carries that assumption.

Why a table in code rather than provider APIs?
Provider price endpoints are not standard, need a key for some, and change on
their schedule. A table with an as-of date is auditable: a wrong price is a
one-line diff, reviewed like any other change. Unknown models fall back to the
primary model's price and are flagged `assumed=True` rather than silently
under-reporting.
"""
from __future__ import annotations

from dataclasses import dataclass

# Update this when the table below changes. Shown in the API response so a
# stale estimate is never mistaken for today's bill.
PRICE_TABLE_AS_OF = "2026-11-22"

# Input tokens are ~70% of a research run (page text + notes), output ~30%
# (plans, notes, critique, report). One ratio, stated, instead of a fake split.
OUTPUT_SHARE = 0.30


@dataclass(frozen=True)
class Price:
    """USD per 1M tokens, from the provider's public list price."""

    input_usd_per_mtok: float
    output_usd_per_mtok: float
    label: str

    @property
    def blended_usd_per_mtok(self) -> float:
        return round(
            self.input_usd_per_mtok * (1 - OUTPUT_SHARE)
            + self.output_usd_per_mtok * OUTPUT_SHARE,
            4,
        )


# The proxy's virtual names are what the app sees (`reports.model` sometimes
# holds the alias, sometimes the upstream name - both are mapped here).
# Prices: Gemini 2.5 family list prices; Groq gpt-oss-120b list price.
FLASH = Price(0.30, 2.50, "Gemini Flash")
FLASH_LITE = Price(0.10, 0.40, "Gemini Flash-Lite")
GROQ_OSS = Price(0.15, 0.75, "Groq GPT-OSS 120B")

MODELS: dict[str, Price] = {
    # virtual names served by the LiteLLM proxy
    "support-chat": FLASH,
    "support-chat-lite": FLASH_LITE,
    "support-chat-groq": GROQ_OSS,
    # upstream names that can appear in reports.model after a failover
    "gemini/gemini-3.8-flash": FLASH,
    "gemini/gemini-2.5-flash": FLASH,
    "gemini/gemini-2.5-flash-lite": FLASH_LITE,
    "gemini/gemini-3.8-flash-lite": FLASH_LITE,
    "groq/openai/gpt-oss-120b": GROQ_OSS,
    "openai/gpt-oss-120b": GROQ_OSS,
}

# Ordered: "flash-lite" must win before the broader "flash" rule.
_FRAGMENTS: tuple[tuple[str, Price], ...] = (
    ("lite", FLASH_LITE),
    ("gpt-oss", GROQ_OSS),
    ("groq", GROQ_OSS),
    ("flash", FLASH),
)

DEFAULT_MODEL = "support-chat"


def price_for(model: str | None) -> tuple[Price, bool]:
    """Return (price, known). Unknown models use the default and `known=False`."""
    key = (model or "").strip().lower()
    if key in MODELS:
        return MODELS[key], True
    for fragment, price in _FRAGMENTS:
        if fragment in key:
            return price, True
    return MODELS[DEFAULT_MODEL], False


def estimate_usd(tokens: int, model: str | None) -> float:
    """Blended list-price estimate for `tokens`, rounded to 6 decimals."""
    if not tokens or tokens < 0:
        return 0.0
    price, _ = price_for(model)
    return round(tokens / 1_000_000 * price.blended_usd_per_mtok, 6)


def estimate_detail(tokens: int, model: str | None) -> dict:
    """The estimate plus the assumptions behind it (what the API returns)."""
    price, known = price_for(model)
    return {
        "model": model or DEFAULT_MODEL,
        "tokens": int(tokens or 0),
        "usd_per_mtok": price.blended_usd_per_mtok,
        "estimated_usd": estimate_usd(tokens, model),
        "price_label": price.label,
        "assumed": not known,
    }


def pricing_metadata() -> dict:
    """Shown next to the dashboard numbers so nobody quotes a stale estimate."""
    return {
        "as_of": PRICE_TABLE_AS_OF,
        "output_share": OUTPUT_SHARE,
        "basis": "blended list price; the free tier itself is billed $0",
        "currencies": "USD",
    }
