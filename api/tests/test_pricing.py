"""Pricing is arithmetic plus one documented assumption; test the edges."""
from __future__ import annotations

from app.core.pricing import (
    FLASH,
    FLASH_LITE,
    GROQ_OSS,
    estimate_detail,
    estimate_usd,
    price_for,
    pricing_metadata,
)


def test_virtual_and_upstream_names_map_to_the_same_price():
    assert price_for("support-chat-lite") == (FLASH_LITE, True)
    assert price_for("gemini/gemini-2.5-flash-lite") == (FLASH_LITE, True)
    assert price_for("support-chat") == (FLASH, True)
    assert price_for("openai/gpt-oss-120b") == (GROQ_OSS, True)
    assert price_for("support-chat-groq") == (GROQ_OSS, True)


def test_lite_is_not_mistaken_for_flash():
    price, known = price_for("gemini-3.8-flash-lite")
    assert known and price is FLASH_LITE


def test_an_unknown_model_is_flagged_not_silently_priced_as_free():
    price, known = price_for("some-new-model")
    assert known is False
    assert price is FLASH
    detail = estimate_detail(1_000_000, "some-new-model")
    assert detail["assumed"] is True
    assert detail["estimated_usd"] == FLASH.blended_usd_per_mtok


def test_one_million_lite_tokens_costs_its_blended_list_price():
    assert estimate_usd(1_000_000, "support-chat-lite") == 0.19
    assert estimate_usd(500_000, "support-chat-lite") == 0.095


def test_zero_and_negative_tokens_cost_nothing():
    assert estimate_usd(0, "support-chat") == 0.0
    assert estimate_usd(-100, "support-chat") == 0.0


def test_metadata_carries_the_as_of_date_and_the_blend_assumption():
    meta = pricing_metadata()
    assert meta["as_of"]
    assert meta["output_share"] == 0.30
    assert "free tier" in meta["basis"]
