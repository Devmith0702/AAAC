"""Synthetic result records: determinism and stable size."""

from __future__ import annotations

import json

from aaac.origin.records import _SUBJECTS, make_result_record


def test_records_replay_exactly() -> None:
    assert make_result_record(1, 900_123) == make_result_record(1, 900_123)


def test_seed_changes_the_record() -> None:
    assert make_result_record(1, 900_123) != make_result_record(2, 900_123)


def test_record_has_the_shape_promised_in_section_4_1() -> None:
    record = make_result_record(1, 900_123)
    assert record["index_no"] == 900_123
    assert isinstance(record["name"], str) and " " in record["name"]
    assert len(record["results"]) == len(_SUBJECTS)
    assert [r["subject"] for r in record["results"]] == list(_SUBJECTS)
    assert all(r["grade"] in "ABCSW" for r in record["results"])


def test_records_are_json_serialisable_and_small() -> None:
    encoded = json.dumps(make_result_record(1, 900_123))
    assert len(encoded) < 1024


def test_record_size_is_stable_across_indices() -> None:
    # Byte accounting in §4.4 should not be noisy because of name lengths.
    sizes = [len(json.dumps(make_result_record(1, i))) for i in range(2000)]
    assert max(sizes) - min(sizes) < 40


def test_distinct_indices_mostly_produce_distinct_records() -> None:
    records = {json.dumps(make_result_record(1, i)) for i in range(2000)}
    assert len(records) == 2000
