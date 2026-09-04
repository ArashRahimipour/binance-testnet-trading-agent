from decimal import Decimal

from trading_agent.execution.fees import compute_commission_buckets, estimate_fee_from_notional
from trading_agent.execution.testnet_adapter import Fill

QUOTE = "USDT"
BASE = "BTC"


def test_quote_only_commission_is_exact_not_estimated():
    fills = [
        Fill(Decimal(50000), Decimal("0.0005"), Decimal("0.025"), "USDT"),
        Fill(Decimal(50010), Decimal("0.0005"), Decimal("0.025"), "USDT"),
    ]
    commission_quote, commission_base, other, is_estimated = compute_commission_buckets(
        fills, QUOTE, BASE, fallback_notional=Decimal(50), fallback_fee_pct=Decimal("0.001")
    )
    assert commission_quote == Decimal("0.05")
    assert commission_base == Decimal(0)
    assert other == {}
    assert is_estimated is False


def test_base_only_commission_is_exact_not_estimated():
    # The normal, default (no BNB discount) case for a BUY.
    fills = [Fill(Decimal(50000), Decimal("0.001"), Decimal("0.0000015"), "BTC")]
    commission_quote, commission_base, other, is_estimated = compute_commission_buckets(
        fills, QUOTE, BASE, fallback_notional=Decimal(50), fallback_fee_pct=Decimal("0.001")
    )
    assert commission_quote == Decimal(0)
    assert commission_base == Decimal("0.0000015")
    assert other == {}
    assert is_estimated is False


def test_third_asset_commission_recorded_separately_and_still_exact():
    fills = [Fill(Decimal(50000), Decimal("0.001"), Decimal("0.0000015"), "BNB")]
    commission_quote, commission_base, other, is_estimated = compute_commission_buckets(
        fills, QUOTE, BASE, fallback_notional=Decimal(50), fallback_fee_pct=Decimal("0.001")
    )
    assert commission_quote == Decimal(0)
    assert commission_base == Decimal(0)
    assert other == {"BNB": Decimal("0.0000015")}
    assert is_estimated is False  # quote/base accounting is exact; the BNB spend is just untracked


def test_mixed_commission_assets_across_fills_are_summed_by_bucket():
    fills = [
        Fill(Decimal(50000), Decimal("0.0003"), Decimal("0.015"), "USDT"),
        Fill(Decimal(50010), Decimal("0.0003"), Decimal("0.0000009"), "BTC"),
        Fill(Decimal(50020), Decimal("0.0004"), Decimal("0.0000012"), "BNB"),
    ]
    commission_quote, commission_base, other, is_estimated = compute_commission_buckets(
        fills, QUOTE, BASE, fallback_notional=Decimal(50), fallback_fee_pct=Decimal("0.001")
    )
    assert commission_quote == Decimal("0.015")
    assert commission_base == Decimal("0.0000009")
    assert other == {"BNB": Decimal("0.0000012")}
    assert is_estimated is False


def test_no_fills_falls_back_to_notional_based_estimate():
    commission_quote, commission_base, other, is_estimated = compute_commission_buckets(
        [], QUOTE, BASE, fallback_notional=Decimal(100), fallback_fee_pct=Decimal("0.001")
    )
    assert commission_quote == Decimal("0.1")  # notional * fallback_fee_pct, not silently zero
    assert commission_base == Decimal(0)
    assert other == {}
    assert is_estimated is True


def test_estimate_fee_from_notional():
    assert estimate_fee_from_notional(Decimal(100), Decimal("0.001")) == Decimal("0.1")
