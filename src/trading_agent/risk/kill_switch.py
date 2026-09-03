"""A manually controlled, file-based kill switch.

Engaging the kill switch halts ALL order submission - both new BUYs and
EXITs. This is a deliberate, simple design choice: a partial kill switch
that still allows exits under some conditions is harder to reason about
and easier to get subtly wrong. If the switch is engaged, the owner is
expected to manage any open position manually via the Testnet UI.

The switch is just a flag file's presence, so it is trivial to inspect,
version-control-ignore, and operate without any daemon or IPC.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path


class KillSwitch:
    def __init__(self, flag_path: str | Path) -> None:
        self._path = Path(flag_path)

    def engage(self, reason: str = "") -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(UTC).isoformat()
        self._path.write_text(f"{timestamp} {reason}".strip() + "\n")

    def disengage(self) -> None:
        self._path.unlink(missing_ok=True)

    def is_engaged(self) -> bool:
        return self._path.exists()

    def reason(self) -> str | None:
        if not self._path.exists():
            return None
        return self._path.read_text().strip()
