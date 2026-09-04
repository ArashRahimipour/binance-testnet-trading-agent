from trading_agent.journal.journal import Journal


def test_record_and_read_all_entries(tmp_path):
    with Journal(tmp_path / "journal.db") as journal:
        journal.record("SIGNAL", {"type": "buy"}, 1000)
        journal.record("RISK_DECISION", {"approved": True}, 1001)
        entries = journal.all_entries()
    assert len(entries) == 2
    assert entries[0]["entry_type"] == "SIGNAL"
    assert entries[0]["payload"]["type"] == "buy"
    assert entries[1]["timestamp_ms"] == 1001


def test_entries_by_type_filters(tmp_path):
    with Journal(tmp_path / "journal.db") as journal:
        journal.record("SIGNAL", {"n": 1}, 1)
        journal.record("SIGNAL", {"n": 2}, 2)
        journal.record("EXCEPTION", {"error": "boom"}, 3)
        signals = journal.entries_by_type("SIGNAL")
        exceptions = journal.entries_by_type("EXCEPTION")
    assert len(signals) == 2
    assert len(exceptions) == 1
    assert exceptions[0]["payload"]["error"] == "boom"


def test_journal_is_append_only_ordering_preserved(tmp_path):
    with Journal(tmp_path / "journal.db") as journal:
        for i in range(5):
            journal.record("SIGNAL", {"i": i}, i)
        entries = journal.all_entries()
    assert [e["payload"]["i"] for e in entries] == [0, 1, 2, 3, 4]


def test_journal_persists_across_reopen(tmp_path):
    db_path = tmp_path / "journal.db"
    with Journal(db_path) as journal:
        journal.record("SIGNAL", {"n": 1}, 1)
    with Journal(db_path) as journal:
        entries = journal.all_entries()
    assert len(entries) == 1
