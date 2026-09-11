"""Render the three payload variants and account for what they cost (C3).

The same exam result, three renderings. `essential` carries the same
INFORMATION as `full` -- index number, name, every subject and grade, the
outcome. What is dropped is presentation, never content.

Cost has two components and both matter:

  bytes        -- what the transfer weighs
  round trips  -- how many requests it takes to complete the page

`essential` is one request by construction. `full` references real
sub-resources, so on a 250 ms link it pays latency on top of its weight. Any
accounting that reports only the document's Content-Length would miss most of
`full` and none of `essential`, which would move the headline gap in our favour
for a purely instrumental reason. `variant_cost()` exists so that cannot happen.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Literal

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from .discovery import discover, inline_style_urls

Variant = Literal["full", "reduced", "essential"]
VARIANTS: tuple[Variant, ...] = ("full", "reduced", "essential")

TEMPLATE_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

#: Prefix the templates use for sub-resources; maps onto STATIC_DIR on disk.
STATIC_PREFIX = "/static/"


def sample_record() -> dict:
    """A representative result record, for budget measurement and tests.

    Deliberately full-sized: nine subjects, every field populated. Measuring a
    variant against a stub or an empty table would make `full` look far lighter
    than it is in production and would flatter the ratio. The candidate is
    fictional -- this is a mock portal, not a real results service.
    """
    return {
        "exam": "General Certificate of Education (Advanced Level)",
        "year": 2026,
        "index_no": "4218866",
        "name": "A. B. C. Wickramaratne",
        "centre": "Central College, Kandy",
        "district": "Kandy",
        "subjects": [
            {"subject": "Combined Mathematics", "medium": "English", "grade": "A"},
            {"subject": "Physics", "medium": "English", "grade": "A"},
            {"subject": "Chemistry", "medium": "English", "grade": "B"},
            {"subject": "General English", "medium": "English", "grade": "A"},
            {"subject": "General Information Technology", "medium": "English", "grade": "A"},
            {"subject": "Common General Test", "medium": "Sinhala", "grade": "B"},
            {"subject": "Information & Communication Technology", "medium": "English", "grade": "A"},
            {"subject": "Economics", "medium": "Sinhala", "grade": "C"},
            {"subject": "Biology", "medium": "English", "grade": "B"},
        ],
        "z_score": "1.8742",
        "district_rank": "37",
        "island_rank": "1,204",
        "outcome": "Qualified for university admission",
        "passed": True,
        "issued": "2026-09-06",
        "reference": "AL2026-4218866-R1",
    }


@lru_cache(maxsize=1)
def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=True,              # result data is user-facing content
        undefined=StrictUndefined,    # a missing field fails loudly, not silently
        trim_blocks=True,
        lstrip_blocks=True,
    )


def render(variant: Variant, record: dict) -> bytes:
    """Render one variant to the exact bytes that go on the wire.

    Returns bytes, not str: Content-Length is a byte count, and M3 computes
    goodput from it, so the encoding step belongs here rather than at the edge.
    """
    if variant not in VARIANTS:
        raise ValueError(f"unknown variant: {variant!r}")
    html = _env().get_template(f"{variant}.html").render(r=record)
    return html.encode("utf-8")


def static_path(url: str) -> Path | None:
    """Map a sub-resource URL onto its file, or None if it is not ours."""
    if not url.startswith(STATIC_PREFIX):
        return None
    name = url[len(STATIC_PREFIX):].split("?")[0].split("#")[0]
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return None
    p = STATIC_DIR / name
    return p if p.is_file() else None


@dataclass(frozen=True)
class VariantCost:
    """What one variant actually costs a client, both ways."""

    variant: str
    document_bytes: int
    sub_resources: tuple[tuple[str, int], ...] = field(default=())
    missing: tuple[str, ...] = field(default=())

    @property
    def sub_resource_bytes(self) -> int:
        return sum(n for _, n in self.sub_resources)

    @property
    def total_bytes(self) -> int:
        return self.document_bytes + self.sub_resource_bytes

    @property
    def requests(self) -> int:
        """Document plus every sub-resource: the round-trip cost."""
        return 1 + len(self.sub_resources)


def variant_cost(variant: Variant, record: dict) -> VariantCost:
    """Total transferred bytes for a variant: document plus sub-resources.

    Uses the same `discover()` the client SDK uses. Sharing that function is
    what keeps the budget check and the measured transfer talking about the
    same set of resources.
    """
    body = render(variant, record)
    subs: list[tuple[str, int]] = []
    missing: list[str] = []

    urls = [r.url for r in discover(body.decode("utf-8"))]
    urls += inline_style_urls(body.decode("utf-8"))

    seen: set[str] = set()
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        p = static_path(url)
        if p is None:
            missing.append(url)
        else:
            subs.append((url, p.stat().st_size))

    return VariantCost(
        variant=variant,
        document_bytes=len(body),
        sub_resources=tuple(subs),
        missing=tuple(missing),
    )
