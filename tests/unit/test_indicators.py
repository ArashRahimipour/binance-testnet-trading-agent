from trading_agent.indicators.moving_averages import ema, sma


def test_ema_length_matches_input():
    values = [float(i) for i in range(30)]
    result = ema(values, period=10)
    assert len(result) == len(values)


def test_ema_is_causal():
    """EMA[k] computed on a truncated series must equal EMA[k] on the full series."""
    values = [100 + i * 0.7 for i in range(50)]
    full = ema(values, period=12)
    truncated = ema(values[:20], period=12)
    for i in range(20):
        assert abs(full[i] - truncated[i]) < 1e-9


def test_sma_basic_average():
    values = [1, 2, 3, 4, 5]
    result = sma(values, period=3)
    assert abs(result[2] - 2.0) < 1e-9
    assert abs(result[3] - 3.0) < 1e-9
    assert abs(result[4] - 4.0) < 1e-9


def test_ema_rejects_non_positive_period():
    import pytest

    with pytest.raises(ValueError):
        ema([1.0, 2.0], period=0)
