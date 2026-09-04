from decimal import Decimal

from trading_agent.execution.fees import (
    compute_confirmed_fee_for_first_application,
    estimate_fee_for_incremental_fill,
)
from trading_agent.execution.testnet_adapter import Fill


def test_all_quote_denominated_fills_are_exact_not_estimated():
    fills = [
        Fill(Decimal(50000), Decimal("0.0005"), Decimal("0.025"), "USDT"),
        Fill(Decimal(50010), Decimal("0.0005"), Decimal("0.025"), "USDT"),
    ]
    fee, is_estimated = compute_confirmed_fee_for_first_application(
        fills, "USDT", fallback_notional=Decimal(50), fallback_fee_pct=Decimal("0.001")
    )
    assert fee == Decimal("0.05")
    assert is_estimated is False


def test_non_quote_commission_is_reported_and_marked_estimated():
    fills = [Fill(Decimal(50000), Decimal("0.001"), Decimal("0.0000015"), "BNB")]
    fee, is_estimated = compute_confirmed_fee_for_first_application(
        fills, "USDT", fallback_notional=Decimal(50), fallback_fee_pct=Decimal("0.001")
    )
    assert fee == Decimal(0)  # the non-quote commission isn't converted or guessed
    assert is_estimated is True


def test_mixed_quote_and_non_quote_fills_sums_only_quote_and_flags_estimated():
    fills = [
        Fill(Decimal(50000), Decimal("0.0005"), Decimal("0.025"), "USDT"),
        Fill(Decimal(50010), Decimal("0.0005"), Decimal("0.0000015"), "BNB"),
    ]
    fee, is_estimated = compute_confirmed_fee_for_first_application(
        fills, "USDT", fallback_notional=Decimal(50), fallback_fee_pct=Decimal("0.001")
    )
    assert fee == Decimal("0.025")
    assert is_estimated is True


def test_no_fills_falls_back_to_notional_based_estimate():
    fee, is_estimated = compute_confirmed_fee_for_first_application(
        [], "USDT", fallback_notional=Decimal(100), fallback_fee_pct=Decimal("0.001")
    )
    assert fee == Decimal("0.1")
    assert is_estimated is True


def test_incremental_fill_is_always_estimated():
    fee, is_estimated = estimate_fee_for_incremental_fill(Decimal(20), Decimal("0.001"))
    assert fee == Decimal("0.02")
    assert is_estimated is True
