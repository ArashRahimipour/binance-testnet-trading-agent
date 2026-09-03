from trading_agent.risk.kill_switch import KillSwitch


def test_kill_switch_starts_disengaged(tmp_path):
    switch = KillSwitch(tmp_path / "KILL_SWITCH")
    assert switch.is_engaged() is False
    assert switch.reason() is None


def test_engage_creates_flag_with_reason(tmp_path):
    switch = KillSwitch(tmp_path / "KILL_SWITCH")
    switch.engage("manual stop: reviewing strategy")
    assert switch.is_engaged() is True
    assert "manual stop: reviewing strategy" in switch.reason()


def test_disengage_removes_flag(tmp_path):
    switch = KillSwitch(tmp_path / "KILL_SWITCH")
    switch.engage("test")
    switch.disengage()
    assert switch.is_engaged() is False


def test_disengage_is_idempotent_when_not_engaged(tmp_path):
    switch = KillSwitch(tmp_path / "KILL_SWITCH")
    switch.disengage()  # must not raise
    assert switch.is_engaged() is False
