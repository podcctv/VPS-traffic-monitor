#!/usr/bin/env python3
"""Minimal central API for VPS traffic monitor.

Provides:
- ingest endpoint with HMAC verification
- node config endpoint: monthly quota, reset day, login verification
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass, asdict
from typing import Dict

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field, HttpUrl, conint, field_validator

app = FastAPI(title="VPS Traffic Monitor Central API", version="0.1.0")


@dataclass
class NodeConfig:
    node_id: str
    monthly_quota_gb: int = 1024
    reset_day: int = 1
    login_verify_enabled: bool = True
    install_script_url: str | None = None
    uninstall_script_url: str | None = None


# demo in-memory stores (MVP)
NODE_CONFIGS: Dict[str, NodeConfig] = {}
NODE_SECRETS: Dict[str, dict] = {
    "demo-key": {"hmac_secret": "demo-secret", "node_id": "demo-node"}
}
INGEST_CACHE = set()


class ConfigUpdate(BaseModel):
    monthly_quota_gb: conint(ge=1, le=1024 * 1024) = Field(..., description="Monthly traffic quota in GB")
    reset_day: conint(ge=1, le=31)
    login_verify_enabled: bool
    install_script_url: HttpUrl | None = None
    uninstall_script_url: HttpUrl | None = None

    @field_validator("install_script_url", "uninstall_script_url")
    @classmethod
    def enforce_https(cls, value: HttpUrl | None) -> HttpUrl | None:
        if value is None:
            return value
        if value.scheme != "https":
            raise ValueError("script url must use https")
        return value


class IngestPayload(BaseModel):
    node_id: str
    timestamp: str
    nonce: str
    iface: str
    counters: dict
    hourly: list
    daily: list
    hostname: str | None = None
    agent_version: str | None = None


def verify_sig(secret: str, timestamp: str, nonce: str, body: bytes, signature: str) -> bool:
    msg = f"{timestamp}.{nonce}.".encode() + body
    expected = hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


@app.get("/api/v1/nodes/{node_id}/config")
def get_node_config(node_id: str):
    cfg = NODE_CONFIGS.get(node_id) or NodeConfig(node_id=node_id)
    NODE_CONFIGS[node_id] = cfg
    return asdict(cfg)


@app.put("/api/v1/nodes/{node_id}/config")
def update_node_config(node_id: str, update: ConfigUpdate):
    cfg = NodeConfig(node_id=node_id, **update.model_dump())
    NODE_CONFIGS[node_id] = cfg
    return {"ok": True, "config": asdict(cfg)}


@app.post("/api/v1/ingest")
def ingest(
    payload: IngestPayload,
    x_api_key: str = Header(...),
    x_timestamp: str = Header(...),
    x_nonce: str = Header(...),
    x_signature: str = Header(...),
):
    key_cfg = NODE_SECRETS.get(x_api_key)
    if not key_cfg:
        raise HTTPException(status_code=401, detail="invalid api key")

    if key_cfg["node_id"] != payload.node_id:
        raise HTTPException(status_code=403, detail="api key not allowed for node")

    now = int(time.time())
    ts = int(time.mktime(time.strptime(x_timestamp.replace("Z", ""), "%Y-%m-%dT%H:%M:%S")))
    if abs(now - ts) > 300:
        raise HTTPException(status_code=401, detail="timestamp outside 5-minute window")

    dedupe_key = f"{payload.node_id}:{x_timestamp}:{x_nonce}"
    if dedupe_key in INGEST_CACHE:
        return {"ok": True, "deduped": True}

    body = json.dumps(payload.model_dump(), separators=(",", ":")).encode()
    if not verify_sig(key_cfg["hmac_secret"], x_timestamp, x_nonce, body, x_signature):
        raise HTTPException(status_code=401, detail="bad signature")

    INGEST_CACHE.add(dedupe_key)
    return {"ok": True, "stored": True}
