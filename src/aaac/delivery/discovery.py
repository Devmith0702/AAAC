"""Sub-resource discovery: what a browser would fetch after the document.

THIS MODULE IS SHARED ON PURPOSE.

Both the client SDK (which fetches sub-resources to measure real transfer cost)
and the payload-budget check (which sums a variant's total bytes) call
`discover()`. If they each had their own idea of what a page pulls, the budget
numbers and the measured numbers would drift apart silently, and the ratio test
would be verifying the checker against itself rather than against reality.

Parsing is done with html.parser, never a regex (section 4.5). A stray
`<script src=...>` inside a comment or an unusual attribute order will defeat a
pattern match; it will not defeat a parser.
"""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser

#: <link rel> values a browser actually issues a request for. Deliberately a
#: whitelist: `canonical`, `alternate`, `dns-prefetch` and `preconnect` name a
#: URL without fetching a body, and counting them would inflate the byte total.
FETCHING_LINK_RELS = frozenset(
    {"stylesheet", "preload", "icon", "shortcut icon", "apple-touch-icon", "manifest"}
)

#: (tag, attribute) pairs whose value is a URL the browser retrieves.
URL_ATTRS: tuple[tuple[str, str], ...] = (
    ("script", "src"),
    ("img", "src"),
    ("image", "href"),
    ("source", "src"),
    ("video", "src"),
    ("video", "poster"),
    ("audio", "src"),
    ("track", "src"),
    ("iframe", "src"),
    ("embed", "src"),
    ("object", "data"),
    ("input", "src"),
)


@dataclass(frozen=True)
class SubResource:
    """One thing the document causes the browser to fetch."""

    url: str
    tag: str
    attr: str


class _Collector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.found: list[SubResource] = []
        self.inline_style_css: list[str] = []
        self._in_style = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}

        if tag == "style":
            self._in_style = True
            return

        if tag == "link":
            href = a.get("href", "").strip()
            rel = " ".join(a.get("rel", "").lower().split())
            if href and rel in FETCHING_LINK_RELS:
                self.found.append(SubResource(href, "link", "href"))
            return

        for t, attr in URL_ATTRS:
            if tag == t:
                # <input src> only fetches for type="image".
                if tag == "input" and a.get("type", "").lower() != "image":
                    continue
                val = a.get(attr, "").strip()
                if val:
                    self.found.append(SubResource(val, tag, attr))

        # srcset carries several candidates; a browser fetches one, but for a
        # byte budget every candidate is a resource the page can pull.
        srcset = a.get("srcset", "").strip()
        if srcset and tag in {"img", "source"}:
            for candidate in srcset.split(","):
                url = candidate.strip().split(" ")[0].strip()
                if url:
                    self.found.append(SubResource(url, tag, "srcset"))

    def handle_endtag(self, tag: str) -> None:
        if tag == "style":
            self._in_style = False

    def handle_data(self, data: str) -> None:
        if self._in_style:
            self.inline_style_css.append(data)


def _parse(html: str) -> _Collector:
    c = _Collector()
    c.feed(html)
    c.close()
    return c


def discover(html: str) -> list[SubResource]:
    """Every sub-resource the document references, in document order.

    Deduplicated by URL: a browser fetches a repeated URL once and serves the
    rest from cache, so counting it twice would overstate the transfer.
    """
    seen: set[str] = set()
    out: list[SubResource] = []
    for r in _parse(html).found:
        if r.url not in seen:
            seen.add(r.url)
            out.append(r)
    return out


def discover_urls(html: str) -> list[str]:
    """Just the URLs, for callers that do not care where they came from."""
    return [r.url for r in discover(html)]


def inline_style_urls(html: str) -> list[str]:
    """URLs referenced from inside inline <style> blocks.

    `essential` must make exactly one HTTP request, and an `@import` or a
    `url(...)` in an inline stylesheet is a request like any other -- it just
    does not appear as an element. Extraction is parser-driven; only the
    already-isolated CSS text is scanned.
    """
    urls: list[str] = []
    for css in _parse(html).inline_style_css:
        rest = css
        while "url(" in rest:
            _, _, rest = rest.partition("url(")
            value, _, rest = rest.partition(")")
            v = value.strip().strip("\"'").strip()
            # A data: URI is inline bytes, not a network fetch.
            if v and not v.lower().startswith("data:"):
                urls.append(v)
        for chunk in css.split("@import")[1:]:
            line = chunk.split(";")[0].strip()
            v = line.split("url(")[-1].strip("() \"'")
            if v and not v.lower().startswith("data:"):
                urls.append(v)
    return urls


def external_request_count(html: str) -> int:
    """Total network requests the document triggers beyond itself."""
    return len(discover(html)) + len(inline_style_urls(html))
