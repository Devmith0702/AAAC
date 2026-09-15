"""Synthetic result records: determinism, stable size, and renderability.

The last one is the reason this file imports M2's delivery package. The origin
used to emit a record with the three fields CLAUDE.md §4.1 names, M2's templates
render fourteen, and every unit test on both sides passed anyway — M2 rendered
its own `sample_record()` and M3 never rendered anything. The mismatch only
appeared when a real client fetched a real page and the delivery service
answered 500 (`'dict object' has no attribute 'exam'`, Jinja `StrictUndefined`).
So the contract between the two now has a test on it.
"""

from __future__ import annotations

import html
import json

import pytest

from aaac.delivery.variants import VARIANTS, render
from aaac.origin.records import _SUBJECTS, make_result_record


def test_records_replay_exactly() -> None:
    assert make_result_record(1, 900_123) == make_result_record(1, 900_123)


def test_seed_changes_the_record() -> None:
    assert make_result_record(1, 900_123) != make_result_record(2, 900_123)


def test_record_has_the_shape_promised_in_section_4_1() -> None:
    record = make_result_record(1, 900_123)
    assert record["index_no"] == 900_123
    assert isinstance(record["name"], str) and " " in record["name"]
    assert len(record["subjects"]) == len(_SUBJECTS)
    assert [s["subject"] for s in record["subjects"]] == [s for s, _ in _SUBJECTS]
    assert [s["medium"] for s in record["subjects"]] == [m for _, m in _SUBJECTS]
    assert all(s["grade"] in "ABCSW" for s in record["subjects"])


@pytest.mark.parametrize("variant", VARIANTS)
def test_every_variant_renders_an_origin_record(variant: str) -> None:
    # The cross-package guard. M2 renders under StrictUndefined, so any field
    # the templates name and the origin omits is a 500 at delivery time and a
    # client that transfers nothing. Rendering here fails the build instead.
    body = render(variant, make_result_record(1, 900_123))
    assert body, f"{variant} rendered empty"
    assert b"<html" in body.lower() or b"<!doctype" in body.lower()


def test_record_carries_the_grades_it_renders() -> None:
    record = make_result_record(1, 900_123)
    # Unescaped: the templates autoescape, so "Information & Communication
    # Technology" reaches the page as "...&amp;...".
    body = html.unescape(render("essential", record).decode("utf-8"))
    for subject in record["subjects"]:
        assert subject["subject"] in body
        assert subject["grade"] in body


def test_records_are_json_serialisable_and_small() -> None:
    # Was < 1 KB when the record held three fields. It now carries the full set
    # the result page renders, which is ~1 KB; the ceiling is here to catch a
    # record that grows into something that would distort byte accounting
    # against the 6 KB essential variant, not to pin an exact size.
    encoded = json.dumps(make_result_record(1, 900_123))
    assert len(encoded) < 2048


def test_record_size_is_stable_across_indices() -> None:
    # Byte accounting in §4.4 should not be noisy because of name lengths.
    sizes = [len(json.dumps(make_result_record(1, i))) for i in range(2000)]
    assert max(sizes) - min(sizes) < 40


def test_distinct_indices_mostly_produce_distinct_records() -> None:
    records = {json.dumps(make_result_record(1, i)) for i in range(2000)}
    assert len(records) == 2000
