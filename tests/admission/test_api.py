import pytest
import asyncio
from fastapi.testclient import TestClient
from aaac.admission.api import app
import aaac.admission.api as api
from aaac.admission.store import InMemoryQueueStore
from aaac.common.config import get_config
from aaac.common.events import EventLogger
from aaac.admission.controller import AdmissionController
from aaac.common.classes import AccessClass

client = TestClient(app)

@pytest.fixture
async def setup_api():
    cfg = get_config()
    api.cfg = cfg
    api.store = InMemoryQueueStore(cfg.run_id)
    api.logger = EventLogger(cfg.run_id, cfg.mode)
    api.controller = AdmissionController(api.store, api.logger, cfg)
    yield
    await api.logger.close()
    
@pytest.mark.asyncio
async def test_join_endpoint(setup_api):
    response = client.post("/queue/join", json={"client_id": "c1", "true_class": 0})
    assert response.status_code == 200
    data = response.json()
    assert "ticket_id" in data
    assert data["join_seq"] == 1
    assert data["position"] == 0
    assert data["eta_s"] == 0.0

@pytest.mark.asyncio
async def test_estimate_endpoint(setup_api):
    # Join first
    join_res = client.post("/queue/join", json={"client_id": "c1", "true_class": 0})
    tid = join_res.json()["ticket_id"]
    
    # Estimate
    est_req = {
        "ticket_id": tid,
        "throughput_kbps": 1000.0,
        "rtt_mean_ms": 50.0,
        "rtt_jitter_ms": 10.0,
        "loss_ratio": 0.0,
        "stability": 1.0,
        "access_class": 0, # HIGH
        "confidence": 0.9,
        "model_version": "v1",
        "fallback": False
    }
    est_res = client.post("/queue/estimate", json=est_req)
    assert est_res.status_code == 200
    assert est_res.json() == {"accepted": True, "access_class": 0}
    
    # Check if class was updated in store
    ticket = await api.store.get_ticket(tid)
    assert ticket.access_class == AccessClass.HIGH

@pytest.mark.asyncio
async def test_status_endpoint(setup_api):
    join_res = client.post("/queue/join", json={"client_id": "c1", "true_class": 0})
    tid = join_res.json()["ticket_id"]
    
    status_res = client.get(f"/queue/status/{tid}")
    assert status_res.status_code == 200
    data = status_res.json()
    assert data["ticket_id"] == tid
    assert data["state"] == "WAITING"
    assert data["position"] == 0
    assert data["admit_token"] is None

@pytest.mark.asyncio
async def test_complete_endpoint(setup_api):
    join_res = client.post("/queue/join", json={"client_id": "c1", "true_class": 0})
    tid = join_res.json()["ticket_id"]
    
    comp_req = {
        "ticket_id": tid,
        "ok": True,
        "bytes": 1024,
        "duration_ms": 500,
        "variant": "full"
    }
    comp_res = client.post("/queue/complete", json=comp_req)
    assert comp_res.status_code == 200
    assert comp_res.json() == {"state": "COMPLETED"}
    
    ticket = await api.store.get_ticket(tid)
    assert ticket.state == "COMPLETED"

@pytest.mark.asyncio
async def test_snapshot_endpoint(setup_api):
    res = client.get("/admin/snapshot")
    assert res.status_code == 200
    data = res.json()
    assert data["run_id"] == api.cfg.run_id
    assert "waiting" in data
    assert "completed" in data
    assert "timed_out" in data
