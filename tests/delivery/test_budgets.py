"""C3 payload budgets, the ratio claim, and the essential single-request rule.

These are the evidence for the C3 claim in the write-up, so they run in CI and
fail the build rather than living only in a skill someone remembers to invoke.

The cross-check at the bottom is the important one. `payload-budget` and the
client SDK must agree about what a variant costs; they share `discover()` so
they agree today, and this test is what keeps them agreeing after someone edits
one of them. A divergence is a hard failure, not a warning: if they drift, the
ratio test would be verifying the checker against itself.

No Redis, no network -- the SDK is driven against the ASGI app in-process
through httpx's ASGI transport (contract rule 4).
"""

from __future__ import annotations

import httpx
import pytest

from aaac.client.fetch import fetch_page
from aaac.common.classes import AccessClass
from aaac.common.tokens import issue_token
from aaac.delivery import app as delivery_app
from aaac.delivery.discovery import (
    discover,
    external_request_count,
    inline_style_urls,
)
from aaac.delivery.variants import VARIANTS, render, sample_record, variant_cost

# configs/run.yaml -> delivery.budgets_bytes. Read from config rather than
# hardcoded, so a budget change has to happen in one place.
from aaac.common.config import get_config

RECORD = sample_record()


@pytest.fixture(scope="module")
def budgets() -> dict[str, int]:
    return dict(get_config().delivery.budgets_bytes)


@pytest.fixture(scope="module")
def costs() -> dict:
    return {v: variant_cost(v, RECORD) for v in VARIANTS}


# --- budgets --------------------------------------------------------------


@pytest.mark.parametrize("variant", VARIANTS)
def test_variant_is_inside_its_budget(variant, costs, budgets):
    """Total transferred bytes, document plus sub-resources -- not the document."""
    c = costs[variant]
    assert not c.missing, f"{variant} references resources we do not serve: {c.missing}"
    assert c.total_bytes <= budgets[variant], (
        f"{variant}: {c.total_bytes:,} bytes over the {budgets[variant]:,} budget"
    )


def test_full_to_essential_ratio(costs):
    """The order-of-magnitude claim, computed on totals.

    Note what this does and does not show. It is a regression guard against
    `essential` creeping upward, not the headline result -- we chose both
    numbers. The honest claim for the write-up is that a realistic styled page
    costs orders of magnitude more than the information itself weighs.
    """
    ratio = costs["full"].total_bytes / costs["essential"].total_bytes
    assert ratio >= 10.0, f"ratio {ratio:.2f}x has fallen below 10x"


def test_variants_are_ordered_by_cost(costs):
    assert (
        costs["essential"].total_bytes
        < costs["reduced"].total_bytes
        < costs["full"].total_bytes
    )


# --- the single-request rule ----------------------------------------------


def test_essential_makes_exactly_one_request():
    """No sub-resource of any kind. Parsed, not pattern-matched.

    On a 250 ms link a second round trip costs more than the bytes it saves, so
    this is the property that makes `essential` worth having at all.
    """
    html = render("essential", RECORD).decode("utf-8")
    assert discover(html) == [], f"essential pulls sub-resources: {discover(html)}"
    assert inline_style_urls(html) == [], "essential's inline CSS references a URL"
    assert external_request_count(html) == 0


def test_reduced_makes_exactly_one_request():
    """`reduced` drops the sub-resources too -- that is most of what it buys."""
    html = render("reduced", RECORD).decode("utf-8")
    assert external_request_count(html) == 0


def test_essential_has_no_sub_resource_elements():
    """Belt and braces: the specific tags section 4.2 names, by parsing."""
    from html.parser import HTMLParser

    banned = {"script", "img", "iframe", "object", "embed", "source", "video", "audio"}
    found: list[str] = []

    class P(HTMLParser):
        def handle_starttag(self, tag, attrs):
            a = dict(attrs)
            if tag in banned:
                found.append(tag)
            if tag == "link":
                found.append(f"link[rel={a.get('rel')}]")

    p = P()
    p.feed(render("essential", RECORD).decode("utf-8"))
    p.close()
    assert found == [], f"essential contains {found}"


def test_full_does_reference_sub_resources():
    """The contract change exists for this; if it stops being true, say so."""
    html = render("full", RECORD).decode("utf-8")
    urls = [r.url for r in discover(html)]
    assert len(urls) >= 4, f"full should pull real sub-resources, got {urls}"
    assert any(u.endswith(".css") for u in urls)
    assert any(u.endswith(".js") for u in urls)
    assert any(u.endswith(".woff2") for u in urls)
    assert any(u.endswith(".png") for u in urls)


# --- content parity -------------------------------------------------------


def _rendered_text(variant: str, record: dict) -> str:
    """Rendered output as a reader sees it: entities resolved.

    A subject named "Information & Communication Technology" is correctly
    escaped to `&amp;`, so asserting against raw markup fails on correct
    behaviour. Compare text, not tags.
    """
    import html as html_mod

    return html_mod.unescape(render(variant, record).decode("utf-8"))


def _subject_rows(variant: str, record: dict) -> set[tuple[str, ...]]:
    """Every table row, as a tuple of its cell texts.

    Structured extraction rather than substring search: it proves the subject
    and its grade appear *together on one row*, which is the property that
    actually matters. A page listing every subject and, separately, every
    grade would satisfy a substring check and be useless to a student.
    """
    from html.parser import HTMLParser

    class Rows(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.rows: list[list[str]] = []
            self._cell: list[str] | None = None
            self._buf: list[str] = []
            self._in_cell = False

        def handle_starttag(self, tag, attrs):
            if tag == "tr":
                self._cell = []
            elif tag in ("td", "th") and self._cell is not None:
                self._in_cell, self._buf = True, []

        def handle_endtag(self, tag):
            if tag in ("td", "th") and self._in_cell and self._cell is not None:
                self._cell.append("".join(self._buf).strip())
                self._in_cell = False
            elif tag == "tr" and self._cell:
                self.rows.append(self._cell)
                self._cell = None

        def handle_data(self, data):
            if self._in_cell:
                self._buf.append(data)

    p = Rows()
    p.feed(render(variant, record).decode("utf-8"))
    p.close()
    return {tuple(r) for r in p.rows}


@pytest.mark.parametrize("variant", VARIANTS)
def test_every_variant_renders_every_field(variant):
    """Every scalar in the record reaches the page -- proved by substitution.

    Each field is rendered with a unique sentinel and the sentinel is looked
    for. A plain substring check cannot do this job: the district "Kandy" also
    occurs inside the centre "Central College, Kandy", and a grade of "A"
    occurs in almost any HTML, so both would pass whether or not the field was
    ever rendered. Those are tests that cannot fail, which is worse than no
    test at all.
    """
    scalars = [k for k, v in RECORD.items() if not isinstance(v, (list, bool))]
    for field_name in scalars:
        sentinel = f"ZQX{field_name.upper()}SENTINEL"
        record = dict(RECORD)
        record[field_name] = sentinel
        text = _rendered_text(variant, record)
        assert sentinel in text, f"{variant} never renders {field_name!r}"


@pytest.mark.parametrize("variant", VARIANTS)
def test_every_variant_renders_every_subject_row(variant):
    """Subject, medium and grade appear together, on the same row."""
    rows = _subject_rows(variant, RECORD)
    for s in RECORD["subjects"]:
        assert any(
            s["subject"] in r and s["medium"] in r and s["grade"] in r for r in rows
        ), f"{variant} has no row pairing {s['subject']} with {s['grade']}"


def test_essential_carries_the_same_information_as_full():
    """`essential` drops presentation, never content (section 4.2).

    This is the claim that justifies serving 1.5 KB to a struggling client
    instead of 400 KB. If `essential` quietly omitted the island rank, we would
    be shipping a worse result to exactly the students the project exists to
    serve.
    """
    assert _subject_rows("essential", RECORD) == _subject_rows("full", RECORD)

    essential = _rendered_text("essential", RECORD)
    for field_name, value in RECORD.items():
        if isinstance(value, (list, bool)):
            continue
        sentinel = f"ZQX{field_name.upper()}SENTINEL"
        record = dict(RECORD)
        record[field_name] = sentinel
        assert sentinel in _rendered_text("essential", record), (
            f"essential omits {field_name!r}, which `full` renders"
        )
    assert essential  # rendered non-empty


# --- SDK / budget cross-check ---------------------------------------------


def _client() -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=delivery_app.app)
    return httpx.AsyncClient(transport=transport, base_url="http://delivery:8001")


@pytest.fixture(autouse=True)
def _stub_origin(monkeypatch):
    """The origin is M3's service and does not exist here. Substitute the record."""
    async def fake(index: str) -> dict:
        return RECORD

    monkeypatch.setattr(delivery_app, "fetch_record", fake)


@pytest.mark.parametrize(
    "access_class,variant",
    [(AccessClass.HIGH, "full"), (AccessClass.MEDIUM, "reduced"), (AccessClass.LOW, "essential")],
)
async def test_sdk_total_matches_budget_total(access_class, variant, costs):
    """What the SDK measures must equal what payload-budget computes.

    Shared code makes them agree today; this assertion makes them stay agreeing.
    A divergence means one of them has changed its mind about what a page pulls,
    and every byte number downstream becomes untrustworthy.
    """
    token = issue_token("t-budget", access_class, 1, ttl_s=60)
    async with _client() as client:
        t = await fetch_page(client, f"/result?token={token}&index=4218866")

    assert t.ok, f"transfer failed: {t.failed_sub_resources}"
    assert t.variant == variant

    expected = costs[variant]
    assert t.document_bytes == expected.document_bytes
    assert t.sub_resource_bytes == expected.sub_resource_bytes
    assert t.bytes == expected.total_bytes, (
        f"{variant}: SDK measured {t.bytes:,} bytes, "
        f"payload-budget computed {expected.total_bytes:,}"
    )
    assert t.requests == expected.requests


async def test_sdk_reports_request_counts_that_match_the_design():
    """essential and reduced are one request; full is the document plus assets."""
    async with _client() as client:
        low = await fetch_page(
            client,
            f"/result?token={issue_token('t1', AccessClass.LOW, 1, 60)}&index=1",
        )
        high = await fetch_page(
            client,
            f"/result?token={issue_token('t2', AccessClass.HIGH, 1, 60)}&index=1",
        )
    assert low.requests == 1
    assert high.requests > 1


# --- token is the only source of the variant ------------------------------


async def test_tampered_token_is_rejected():
    token = issue_token("t-tamper", AccessClass.LOW, 1, ttl_s=60)
    payload, _, sig = token.partition(".")
    async with _client() as client:
        r = await client.get(f"/result?token={payload}.{sig[:-2]}xx&index=1")
    assert r.status_code == 401


async def test_expired_token_is_rejected():
    token = issue_token("t-exp", AccessClass.HIGH, 1, ttl_s=-1)
    async with _client() as client:
        r = await client.get(f"/result?token={token}&index=1")
    assert r.status_code == 401


async def test_variant_cannot_be_forced_by_query_parameter():
    """A LOW token asking for `full` still gets `essential`.

    The variant comes from the verified token and nowhere else (section 3.7).
    If a query parameter could override it, admission control would be advisory.
    """
    token = issue_token("t-force", AccessClass.LOW, 1, ttl_s=60)
    async with _client() as client:
        r = await client.get(
            f"/result?token={token}&index=1&variant=full&var=full&class=0"
        )
    assert r.status_code == 200
    assert r.headers["X-AAAC-Variant"] == "essential"
    assert external_request_count(r.text) == 0


# --- Content-Length accuracy ----------------------------------------------


@pytest.mark.parametrize("access_class", [AccessClass.HIGH, AccessClass.MEDIUM, AccessClass.LOW])
async def test_content_length_is_accurate(access_class):
    """M3 computes goodput from Content-Length, so it must equal the body."""
    token = issue_token("t-cl", access_class, 1, ttl_s=60)
    async with _client() as client:
        r = await client.get(f"/result?token={token}&index=1")
        assert int(r.headers["content-length"]) == len(r.content)

        for sub in discover(r.text):
            s = await client.get(sub.url)
            assert s.status_code == 200, f"{sub.url} -> {s.status_code}"
            assert int(s.headers["content-length"]) == len(s.content)
