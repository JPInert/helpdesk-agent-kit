"""Queue triage and the seen registry (inventory item 9)."""

from __future__ import annotations

from datetime import datetime, timezone

from helpdesk_kit import db, triage
from helpdesk_kit.classify import TicketType
from helpdesk_kit.zendesk import FixtureSource

from conftest import FIXTURES

NOW = datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc)


def item(tid, created, kind=TicketType.LICENSE_NOT_ASSIGNED, urgency=2, comments=1):
    return triage.QueueItem(tid, created, None, urgency, kind, comments)


def test_urgency_comes_from_priority_and_urgent_words_bump_it_one_level():
    assert triage.urgency("urgent", "") == 0 and triage.urgency("high", "") == 1
    assert triage.urgency(None, "") == 2 and triage.urgency("low", "") == 3
    assert triage.urgency(None, "I need it today") == 1 and triage.urgency("urgent", "asap") == 0
    assert triage.urgency("low", "deadline") == 2


def test_order_is_urgency_then_type_then_oldest_first():
    items = [item("a", "2026-10-05T09:00:00Z", TicketType.HARDWARE),
             item("b", "2026-10-05T10:00:00Z", TicketType.LICENSE_NOT_ASSIGNED),
             item("c", "2026-10-05T08:00:00Z", TicketType.LICENSE_NOT_ASSIGNED),
             item("d", "2026-10-05T11:00:00Z", TicketType.POLICY_QUESTION, urgency=0),
             item("e", "2026-10-05T07:00:00Z", TicketType.ACCOUNT_SUSPENDED)]
    assert [i.ticket_id for i in triage.order(items, NOW)] == ["d", "e", "c", "b", "a"]


def test_quick_kind_is_a_cheap_keyword_guess():
    assert triage.quick_kind("The 3rd floor printer is jammed") == TicketType.HARDWARE
    assert triage.quick_kind("my session was rejected") == TicketType.SESSION_REJECTED
    assert triage.quick_kind("hello") == TicketType.AMBIGUOUS


def test_harvest_reads_cheap_fields_for_every_open_ticket():
    items = {i.ticket_id: i for i in triage.harvest(FixtureSource(FIXTURES))}
    assert len(items) == 19 and items["1015"].priority == "urgent" and items["1015"].urgency == 0
    assert items["1009"].kind == TicketType.HARDWARE and items["1012"].kind == TicketType.SESSION_REJECTED


def test_seen_registry_skips_what_was_processed_and_lets_new_comments_through():
    conn = db.connect(":memory:")
    assert not triage.is_seen(conn, "1", 1)
    triage.mark_seen(conn, "1", 1, "SENT")
    assert triage.is_seen(conn, "1", 1) and not triage.is_seen(conn, "1", 2)
    items = [item("1", "2026-10-05T09:00:00Z"), item("2", "2026-10-05T09:00:00Z"), item("1", "x", comments=2)]
    assert [(i.ticket_id, i.comments) for i in triage.unseen(conn, items)] == [("2", 1), ("1", 2)]


def test_nothing_is_processed_twice_across_two_queue_runs():
    conn, source, done = db.connect(":memory:"), FixtureSource(FIXTURES), []
    for _ in range(2):
        items = triage.order(triage.unseen(conn, triage.harvest(source)))
        run = triage.new_run(conn, items)
        triage.work(conn, source, run, lambda tid: done.append(tid))
    assert len(done) == 19 and len(set(done)) == 19


def test_a_run_is_a_job_table_and_each_job_ends_done():
    conn, source = db.connect(":memory:"), FixtureSource(FIXTURES)
    items = triage.order(triage.harvest(source))[:3]
    run = triage.new_run(conn, items)
    assert triage.job_counts(conn, run) == {"PENDING": 3}
    triage.work(conn, source, run, lambda tid: None)
    assert triage.job_counts(conn, run) == {"DONE": 3} and triage.latest_run(conn) == run


def test_an_interrupted_run_resumes_where_it_stopped():
    conn, source = db.connect(":memory:"), FixtureSource(FIXTURES)
    items = triage.order(triage.harvest(source))[:4]
    run = triage.new_run(conn, items)
    order, seen_in_first = [i.ticket_id for i in items], []

    def crash_on_third(tid):
        seen_in_first.append(tid)
        if len(seen_in_first) == 3:
            raise KeyboardInterrupt                     # the process dies mid-ticket
    try:
        triage.work(conn, source, run, crash_on_third)
    except KeyboardInterrupt:
        pass
    counts = triage.job_counts(conn, run)
    assert counts == {"DONE": 2, "RUNNING": 1, "PENDING": 1}
    resumed = []
    triage.work(conn, source, run, resumed.append)
    assert resumed == order[2:] and triage.job_counts(conn, run) == {"DONE": 4}      # the RUNNING job is redone


def test_one_failing_ticket_is_marked_failed_and_the_queue_carries_on():
    conn, source = db.connect(":memory:"), FixtureSource(FIXTURES)
    items = triage.order(triage.harvest(source))[:3]
    run = triage.new_run(conn, items)

    def process(tid):
        if tid == items[1].ticket_id:
            raise RuntimeError("boom")
    triage.work(conn, source, run, process)
    assert triage.job_counts(conn, run) == {"DONE": 2, "FAILED": 1}
    row = conn.execute("SELECT error FROM queue_jobs WHERE state='FAILED'").fetchone()
    assert row["error"] == "RuntimeError"
    assert not triage.is_seen(conn, items[1].ticket_id, 1)         # a failed ticket is not marked seen
