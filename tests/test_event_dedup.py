"""Tests for CORE-008: EventDeduplicator."""

from ops_engine.core.event_dedup import EventDeduplicator


def test_first_event_is_not_duplicate():
    dedup = EventDeduplicator()
    assert dedup.is_duplicate("delivery-001") is False


def test_second_same_event_is_duplicate():
    dedup = EventDeduplicator()
    dedup.is_duplicate("delivery-001")
    assert dedup.is_duplicate("delivery-001") is True


def test_different_events_are_not_duplicates():
    dedup = EventDeduplicator()
    dedup.is_duplicate("delivery-001")
    assert dedup.is_duplicate("delivery-002") is False


def test_none_delivery_id_is_never_duplicate():
    dedup = EventDeduplicator()
    assert dedup.is_duplicate(None) is False
    assert dedup.is_duplicate(None) is False


def test_empty_string_is_never_duplicate():
    dedup = EventDeduplicator()
    assert dedup.is_duplicate("") is False


def test_extract_github_delivery_id():
    dedup = EventDeduplicator()
    headers = {"x-github-delivery": "gh-123"}
    assert dedup.extract_delivery_id(headers) == "gh-123"


def test_extract_forgejo_delivery_id():
    dedup = EventDeduplicator()
    headers = {"x-forgejo-delivery": "fj-456"}
    assert dedup.extract_delivery_id(headers) == "fj-456"


def test_extract_gitea_delivery_id():
    dedup = EventDeduplicator()
    headers = {"x-gitea-delivery": "gt-789"}
    assert dedup.extract_delivery_id(headers) == "gt-789"


def test_size_tracking():
    dedup = EventDeduplicator()
    assert dedup.size == 0
    dedup.is_duplicate("a")
    dedup.is_duplicate("b")
    assert dedup.size == 2


def test_clear():
    dedup = EventDeduplicator()
    dedup.is_duplicate("a")
    dedup.clear()
    assert dedup.size == 0
    assert dedup.is_duplicate("a") is False
