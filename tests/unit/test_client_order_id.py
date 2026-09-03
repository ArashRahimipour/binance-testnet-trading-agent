from trading_agent.execution.client_order_id import MAX_LENGTH, generate_client_order_id


def test_deterministic_for_same_inputs():
    a = generate_client_order_id("BTCUSDT", "BUY", 1700000000000)
    b = generate_client_order_id("BTCUSDT", "BUY", 1700000000000)
    assert a == b


def test_different_for_different_candle():
    a = generate_client_order_id("BTCUSDT", "BUY", 1700000000000)
    b = generate_client_order_id("BTCUSDT", "BUY", 1700014400000)
    assert a != b


def test_different_for_different_side():
    a = generate_client_order_id("BTCUSDT", "BUY", 1700000000000)
    b = generate_client_order_id("BTCUSDT", "SELL", 1700000000000)
    assert a != b


def test_within_binance_length_limit():
    client_order_id = generate_client_order_id("BTCUSDT", "BUY", 1700000000000)
    assert len(client_order_id) <= MAX_LENGTH


def test_uses_safe_character_set():
    client_order_id = generate_client_order_id("BTCUSDT", "BUY", 1700000000000)
    assert all(c.isalnum() or c in "-_." for c in client_order_id)
