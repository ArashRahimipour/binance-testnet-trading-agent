"""Optional Telegram notifications for shadow mode ONLY.

Nothing in this package ever imports `execution/testnet_adapter.py` or any
other order-placement-capable module, and nothing here ever constructs a
client for any host other than `https://api.telegram.org` (see
`telegram_client.py::TELEGRAM_API_HOST`) - see `tests/unit/
test_shadow_notifications_no_order_placement.py`'s source-level regression
lock. A Telegram failure of any kind - misconfiguration, DNS failure,
timeout, rate limit, malformed response - can never roll back, delay, or
corrupt shadow trading: the trading event is always durably persisted
(`shadow/store.py::ShadowStore.record_cycle_atomically`'s notification
outbox) in the SAME atomic transaction as the trading state it describes,
strictly BEFORE any attempt to actually deliver it - see `sender.py`.
"""

from __future__ import annotations
