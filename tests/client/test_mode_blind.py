"""The client must not be able to tell which run mode it is in (section 3.4).

`none`, `baseline` and `aaac` must run through the same client code paths, with
the difference living entirely in M1's config and behaviour. If the SDK branched
on mode -- or on anything that correlates with it, like a fixed vs scaled window,
or a variant that is always `full` -- the three-way comparison would be invalid
and every number in the evaluation would be worthless.

Convention is not enough for a property this load-bearing, so it is tested two
ways: statically, that the source cannot see mode, and behaviourally, that the
same request sequence is issued against servers behaving as baseline and as
aaac.

The same is done for `true_class` (section 3.8), which is an opaque passthrough
and must never influence a decision the client makes.
"""

from __future__ import annotations

import ast
import json
import time
from pathlib import Path

import httpx
import pytest

from aaac.client import sdk
from aaac.client.sdk import Outcome, run_client
from aaac.common.classes import AccessClass, variant_for
from aaac.common.tokens import issue_token
from aaac.delivery.app import app as delivery_app
from aaac.delivery.variants import sample_record

CLIENT_SRC_DIR = Path(sdk.__file__).parent


# --- static: the client cannot read mode ----------------------------------


MODE_NAMES = {"none", "baseline", "aaac"}


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """id() of every Constant that is a docstring, so prose can be skipped."""
    out: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                out.add(id(body[0].value))
    return out


def test_client_source_never_branches_on_mode():
    """No `mode` as an identifier or literal anywhere in client/ code.

    Checked against the AST, not the text. A substring scan matches
    `model_path`, `model_validate` and every docstring that discusses mode --
    it would fail on prose and pass on `cfg.mode`, which is exactly backwards.
    """
    offenders: list[str] = []

    for path in sorted(CLIENT_SRC_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = _docstring_nodes(tree)

        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "mode":
                offenders.append(f"{path.name}:{node.lineno}: attribute .mode")
            elif isinstance(node, ast.Name) and node.id == "mode":
                offenders.append(f"{path.name}:{node.lineno}: name `mode`")
            elif isinstance(node, ast.keyword) and node.arg == "mode":
                # model_dump(mode="json") is pydantic's serialisation format,
                # not the run mode.
                offenders.append(f"{path.name}: keyword mode= (check it)")
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value in MODE_NAMES and id(node) not in docstrings:
                    offenders.append(
                        f"{path.name}:{node.lineno}: literal {node.value!r}"
                    )

    offenders = [o for o in offenders if "keyword mode=" not in o]
    assert offenders == [], "client branches on mode:\n" + "\n".join(offenders)


def test_client_never_reads_run_config_mode():
    """`get_config().mode` must not be reachable from the client."""
    for path in sorted(CLIENT_SRC_DIR.glob("*.py")):
        src = path.read_text(encoding="utf-8")
        assert "get_config" not in src, f"{path.name} reads the run config"
        assert "RunConfig" not in src, f"{path.name} imports the run config"


def test_client_never_reads_true_class_after_join():
    """true_class is sent once and never consulted (section 3.8)."""
    src = (CLIENT_SRC_DIR / "sdk.py").read_text(encoding="utf-8")
    body = src.split("def run_client", 1)[1]
    uses = [
        line.strip()
        for line in body.splitlines()
        if "true_class" in line.split("#")[0]
    ]
    # Exactly two: the signature parameter, and putting it in the join body.
    assert len(uses) == 2, f"true_class used {len(uses)} times: {uses}"
    assert any("int(true_class)" in u for u in uses)


# --- a mock admission service --------------------------------------------


class MockAdmission:
    """Minimal M1 stand-in that can behave as `baseline` or as `aaac`.

    The differences are exactly the ones a real M1 would show: baseline hands
    everyone a fixed window and a `full` token; aaac scales the window by class
    and serves the variant that class earns. The client must not care.
    """

    def __init__(self, *, behaviour: str, admit_after_polls: int = 6) -> None:
        self.behaviour = behaviour
        self.admit_after_polls = admit_after_polls
        self.requests: list[tuple[str, str]] = []
        self.estimates: list[dict] = []
        self.joins: list[dict] = []
        self.completes: list[dict] = []
        self._polls = 0

    def _record(self, method: str, path: str) -> None:
        self.requests.append((method, path.split("?")[0]))

    @property
    def request_kinds(self) -> list[str]:
        """Request sequence with ticket ids stripped, for comparison."""
        out = []
        for method, path in self.requests:
            if path.startswith("/queue/status/"):
                path = "/queue/status/{tid}"
            out.append(f"{method} {path}")
        return out

    async def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self._record(request.method, path)

        if path == "/queue/join":
            self.joins.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "ticket_id": "t-mock",
                    "join_seq": 1,
                    "position": 3,
                    "eta_s": 4.0,
                    "poll_interval_ms": 1,   # keep the test fast
                },
            )

        if path == "/queue/estimate":
            self.estimates.append(json.loads(request.content))
            return httpx.Response(200, json={"accepted": True, "access_class": 1})

        if path == "/queue/complete":
            self.completes.append(json.loads(request.content))
            return httpx.Response(200, json={"state": "COMPLETED"})

        if path.startswith("/queue/status/"):
            self._polls += 1
            if self._polls < self.admit_after_polls:
                return httpx.Response(
                    200,
                    json={
                        "ticket_id": "t-mock",
                        "state": "WAITING",
                        "position": max(0, self.admit_after_polls - self._polls),
                        "attempt": 1,
                        "access_class": 1,
                        "window_s": None,
                        "admit_token": None,
                        "expires_at": None,
                    },
                )
            cls = AccessClass.HIGH if self.behaviour == "baseline" else AccessClass.LOW
            window = 20.0 if self.behaviour == "baseline" else 50.0
            return httpx.Response(
                200,
                json={
                    "ticket_id": "t-mock",
                    "state": "ADMITTED",
                    "position": 0,
                    "attempt": 1,
                    "access_class": int(cls),
                    "window_s": window,
                    "admit_token": issue_token("t-mock", cls, 1, ttl_s=window),
                    "expires_at": time.time() + window,
                },
            )

        return httpx.Response(404, json={"detail": "no route"})


def _mock_clients(mock: MockAdmission):
    adm = httpx.AsyncClient(
        transport=httpx.MockTransport(mock.handler), base_url="http://admission:8000"
    )
    dlv = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=delivery_app), base_url="http://delivery:8001"
    )
    return adm, dlv


@pytest.fixture(autouse=True)
def _stub_origin(monkeypatch):
    from aaac.delivery import app as delivery_module

    async def fake(index: str) -> dict:
        return sample_record()

    monkeypatch.setattr(delivery_module, "fetch_record", fake)


async def _run_against(mock: MockAdmission, true_class=AccessClass.LOW):
    adm, dlv = _mock_clients(mock)
    try:
        return await run_client(
            client_id="c-1",
            true_class=true_class,
            index_no="4218866",
            admission_base="",
            delivery_base="",
            min_rtt_samples=3,
            abandon_after_s=30.0,
            admission_client=adm,
            delivery_client=dlv,
        )
    finally:
        await adm.aclose()
        await dlv.aclose()


# --- behavioural: identical request sequences -----------------------------


async def test_same_request_sequence_in_baseline_and_aaac():
    """The observable behaviour of the client is identical across modes.

    Only the variant it receives differs, and that is the server's decision
    carried in the token -- not a branch the client took.
    """
    baseline = MockAdmission(behaviour="baseline")
    aaac = MockAdmission(behaviour="aaac")

    a = await _run_against(baseline)
    b = await _run_against(aaac)

    assert baseline.request_kinds == aaac.request_kinds
    assert a.outcome == b.outcome == Outcome.COMPLETED
    assert a.polls == b.polls

    # The variant differs -- because the token said so, not the client.
    assert a.variant == variant_for(AccessClass.HIGH)
    assert b.variant == variant_for(AccessClass.LOW)


async def test_estimate_is_submitted_in_both_modes():
    """The client does not know baseline ignores its estimate, so it still sends
    one. If it skipped the estimate in baseline, it would be branching on mode
    -- and M3 would lose the classifier accuracy figure for the baseline run."""
    for behaviour in ("baseline", "aaac"):
        mock = MockAdmission(behaviour=behaviour)
        await _run_against(mock)
        assert len(mock.estimates) == 1, f"{behaviour} submitted {len(mock.estimates)}"


async def test_true_class_does_not_change_client_behaviour():
    """Passthrough only. Identical request sequences for all three values."""
    sequences = {}
    for tc in (AccessClass.HIGH, AccessClass.MEDIUM, AccessClass.LOW):
        mock = MockAdmission(behaviour="aaac")
        await _run_against(mock, true_class=tc)
        sequences[tc] = mock.request_kinds
    assert sequences[AccessClass.HIGH] == sequences[AccessClass.MEDIUM]
    assert sequences[AccessClass.MEDIUM] == sequences[AccessClass.LOW]


# --- true_class round trip ------------------------------------------------


@pytest.mark.parametrize(
    "true_class", [AccessClass.HIGH, AccessClass.MEDIUM, AccessClass.LOW]
)
async def test_true_class_survives_the_join_round_trip(true_class):
    """M1 types true_class as AccessClass, an IntEnum. It must arrive as its
    exact int value.

    M3 scores classifier accuracy against this field. A string "HIGH" against an
    int enum is a 422 or a silent coercion, and a silently wrong label produces
    an accuracy number that is confidently wrong with nothing erroring.
    """
    mock = MockAdmission(behaviour="aaac")
    await _run_against(mock, true_class=true_class)

    assert len(mock.joins) == 1
    sent = mock.joins[0]["true_class"]
    assert isinstance(sent, int) and not isinstance(sent, bool)
    assert sent == int(true_class)
    assert AccessClass(sent) is true_class


async def test_join_body_carries_client_id_and_true_class():
    mock = MockAdmission(behaviour="aaac")
    await _run_against(mock)
    assert set(mock.joins[0]) == {"client_id", "true_class"}
    assert mock.joins[0]["client_id"] == "c-1"
