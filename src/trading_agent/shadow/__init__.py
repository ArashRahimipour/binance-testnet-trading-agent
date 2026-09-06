"""Forward-only shadow observation of the frozen `multitimeframe_breakout_E1_round3`
candidate (see `research/candidates/multitimeframe_breakout.py`).

This package never places an order - real, Testnet, or otherwise. It fetches
completed Binance BTCUSDT candles from the same read-only public market-data
client every other part of this project already uses
(`data/market_data_public.py`), reuses `backtest/engine.py::run_segment`
UNMODIFIED as its simulation core (so entry/stop/target/fee/slippage/sizing
assumptions are byte-for-byte identical to the already-evaluated E1
candidate), and persists everything to its own database
(`data/shadow_agent.db`, separate from every other database in this
project). See `shadow/engine.py`'s module docstring for the full design.
"""

from __future__ import annotations
