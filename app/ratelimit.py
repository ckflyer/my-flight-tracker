"""Lightweight in-memory rate limiting for auth endpoints (login, register,
password reset). Not distributed — fine for a single-process personal
deployment, resets on restart. That's an acceptable tradeoff here; the goal
is stopping a naive automated script from hammering these endpoints, not
building production-grade abuse protection.
"""
from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Deque, Dict, Tuple

from fastapi import Request

_buckets: Dict[Tuple[str, str], Deque[float]] = defaultdict(deque)


def get_client_ip(request: Request) -> str:
    """Cloudflare puts the real visitor IP in CF-Connecting-IP — without
    this, every request would appear to come from Cloudflare's edge IP and
    the rate limit would lump all visitors together."""
    cf_ip = request.headers.get("cf-connecting-ip")
    if cf_ip:
        return cf_ip.strip()
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def check_rate_limit(request: Request, bucket: str, max_attempts: int, window_seconds: int) -> bool:
    """Returns True if this request should proceed, False if the caller
    has exceeded max_attempts within window_seconds and should be refused."""
    ip = get_client_ip(request)
    key = (bucket, ip)
    now = time.time()
    dq = _buckets[key]
    while dq and now - dq[0] > window_seconds:
        dq.popleft()
    if len(dq) >= max_attempts:
        return False
    dq.append(now)
    return True
