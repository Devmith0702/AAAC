from __future__ import annotations
from pydantic import BaseModel
from typing import Literal
from aaac.common.classes import AccessClass

class LinkSample(BaseModel):
    ticket_id: str
    probe_bytes: int
    probe_duration_ms: float
    rtt_samples_ms: list[float]
    failed_requests: int
    total_requests: int

class LinkEstimate(BaseModel):
    ticket_id: str
    throughput_kbps: float
    rtt_mean_ms: float
    rtt_jitter_ms: float
    loss_ratio: float
    stability: float            # 0..1
    access_class: AccessClass
    confidence: float           # 0..1
    model_version: str
    fallback: bool              # True if defaulted to MEDIUM

class TicketStatus(BaseModel):
    ticket_id: str
    state: Literal["WAITING", "ADMITTED", "COMPLETED", "EXPIRED", "ABANDONED"]
    position: int               # 0 when admitted
    attempt: int                # 1-based
    access_class: AccessClass
    window_s: float | None = None
    admit_token: str | None = None
    expires_at: float | None = None    # unix seconds
