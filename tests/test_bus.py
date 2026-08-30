"""Event bus tests: prefix matching and subscriber isolation."""

from __future__ import annotations

from plp.kernel.bus import EventBus


def test_prefix_matching():
    bus = EventBus()
    seen = []
    bus.subscribe("job.news", lambda e, p: seen.append((e, p)))
    bus.publish("job.news.collect.finished", {"a": 1})
    bus.publish("job.other.finished")
    assert seen == [("job.news.collect.finished", {"a": 1})]


def test_exact_and_prefix_coexist():
    bus = EventBus()
    seen = []
    bus.subscribe("approval.3", lambda e, p: seen.append(("exact", e)))
    bus.subscribe("approval", lambda e, p: seen.append(("prefix", e)))
    bus.publish("approval.3.approved")
    assert ("exact", "approval.3.approved") in seen
    assert ("prefix", "approval.3.approved") in seen


def test_subscriber_exception_isolated():
    bus = EventBus()
    seen = []

    def bad(event, payload):
        raise RuntimeError("subscriber bug")

    bus.subscribe("x", bad)
    bus.subscribe("x", lambda e, p: seen.append(e))
    bus.publish("x.y")
    assert seen == ["x.y"]


def test_no_subscribers_no_error():
    EventBus().publish("anything.at.all", {"z": 1})
