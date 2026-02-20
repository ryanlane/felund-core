from __future__ import annotations

import threading
import time
from typing import Dict, List, Literal, Optional, Tuple

from fastapi import FastAPI, Query, Request
from pydantic import BaseModel, Field, field_validator


TransportType = Literal["tcp", "ws"]
FamilyType = Literal["ipv4", "ipv6"]
NatType = Literal["unknown", "open", "restricted", "symmetric"]


class Endpoint(BaseModel):
    transport: TransportType
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(ge=1, le=65535)
    family: FamilyType = "ipv4"
    nat: NatType = "unknown"


class Capabilities(BaseModel):
    relay: bool = False
    transport: List[TransportType] = Field(default_factory=lambda: ["tcp"])


class RegisterRequest(BaseModel):
    node_id: str = Field(min_length=8, max_length=128)
    circle_hint: str = Field(min_length=8, max_length=128)
    endpoints: List[Endpoint] = Field(default_factory=list)
    capabilities: Capabilities = Field(default_factory=Capabilities)
    ttl_s: int = Field(default=120, ge=30, le=600)

    @field_validator("endpoints")
    @classmethod
    def cap_endpoints(cls, value: List[Endpoint]) -> List[Endpoint]:
        if len(value) > 16:
            raise ValueError("too many endpoints")
        return value


class UnregisterRequest(BaseModel):
    node_id: str = Field(min_length=8, max_length=128)
    circle_hint: str = Field(min_length=8, max_length=128)


class PresenceRecord(BaseModel):
    node_id: str
    circle_hint: str
    endpoints: List[Endpoint]
    capabilities: Capabilities
    observed_at: int
    expires_at: int


class RegisterResponse(BaseModel):
    ok: bool = True
    server_time: int
    expires_at: int
    observed_endpoint: Optional[Endpoint] = None


class PeersResponse(BaseModel):
    ok: bool = True
    peers: List[PresenceRecord]


class HealthResponse(BaseModel):
    ok: bool = True
    version: str
    time: int


class PresenceStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: Dict[Tuple[str, str], PresenceRecord] = {}

    def upsert(
        self,
        req: RegisterRequest,
        observed_host: Optional[str],
        observed_port: Optional[int],
    ) -> PresenceRecord:
        now = int(time.time())
        expires_at = now + req.ttl_s

        with self._lock:
            self._cleanup_locked(now)
            record = PresenceRecord(
                node_id=req.node_id,
                circle_hint=req.circle_hint,
                endpoints=req.endpoints,
                capabilities=req.capabilities,
                observed_at=now,
                expires_at=expires_at,
            )
            self._records[(req.circle_hint, req.node_id)] = record
        return record

    def remove(self, req: UnregisterRequest) -> bool:
        with self._lock:
            return self._records.pop((req.circle_hint, req.node_id), None) is not None

    def list_peers(self, circle_hint: str, exclude_node_id: Optional[str], limit: int) -> List[PresenceRecord]:
        now = int(time.time())
        with self._lock:
            self._cleanup_locked(now)
            peers = [
                record
                for (record_circle, _), record in self._records.items()
                if record_circle == circle_hint
            ]

        if exclude_node_id:
            peers = [record for record in peers if record.node_id != exclude_node_id]

        peers.sort(key=lambda record: record.observed_at, reverse=True)
        return peers[:limit]

    def _cleanup_locked(self, now: int) -> None:
        expired_keys = [key for key, record in self._records.items() if record.expires_at <= now]
        for key in expired_keys:
            self._records.pop(key, None)


app = FastAPI(title="Felund Rendezvous API", version="0.1.0")
store = PresenceStore()


@app.get("/v1/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(version="0.1.0", time=int(time.time()))


@app.post("/v1/register", response_model=RegisterResponse)
async def register(req: RegisterRequest, request: Request) -> RegisterResponse:
    observed_host = request.client.host if request.client else None
    observed_port = request.client.port if request.client else None
    record = store.upsert(req, observed_host, observed_port)

    observed_endpoint = None
    if observed_host and observed_port:
        observed_endpoint = Endpoint(
            transport="tcp",
            host=observed_host,
            port=observed_port,
            family="ipv4" if ":" not in observed_host else "ipv6",
            nat="unknown",
        )

    return RegisterResponse(
        server_time=record.observed_at,
        expires_at=record.expires_at,
        observed_endpoint=observed_endpoint,
    )


@app.get("/v1/peers", response_model=PeersResponse)
async def peers(
    circle_hint: str = Query(min_length=8, max_length=128),
    limit: int = Query(default=50, ge=1, le=200),
    request: Request = None,
) -> PeersResponse:
    caller_node = request.headers.get("X-Felund-Node") if request else None
    records = store.list_peers(circle_hint=circle_hint, exclude_node_id=caller_node, limit=limit)
    return PeersResponse(peers=records)


@app.delete("/v1/register")
async def unregister(req: UnregisterRequest) -> dict:
    removed = store.remove(req)
    return {"ok": True, "removed": removed}
