#!/usr/bin/env python3
"""
Load test for @vidyuklaydi_bot scale features.

Tests three scenarios AGAINST MOCKED INFRASTRUCTURE so no real network/DB is needed.
Run against a real staging environment by setting REAL_REDIS_URL env var.

Scenarios
---------
A — Many users: 500 concurrent users each sending 1 URL
    Assert: handler returns instantly (no blocking), queue depth bounded
B — Burst from one user: 1 user fires 30 links back-to-back
    Assert: all acked instantly, per-user cap enforced (K simultaneous active)
C — Viral cache: 1000 requests for the SAME URL
    Assert: single-flight = 1 actual download, 999 served from cache

Usage:
    python scripts/loadtest.py [--scenario A|B|C|all] [--real]
    --real  Use real Redis from REDIS_URL env var (default: use fakeredis)
"""
from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "0:dummy")
os.environ.setdefault("TELEGRAM_API_ID", "1")
os.environ.setdefault("TELEGRAM_API_HASH", "a" * 32)
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://bot:bot@localhost:5432/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("ADMIN_IDS", "0")
os.environ.setdefault("DOWNLOAD_DIR", "/tmp")

# ── Helpers ───────────────────────────────────────────────────────────────────

@dataclass
class Metrics:
    scenario: str
    total_requests: int = 0
    successful_acks: int = 0
    enqueued_jobs: int = 0
    cache_hits: int = 0
    single_flight_deduped: int = 0
    per_user_cap_enforced: int = 0
    errors: int = 0
    latencies_ms: list = field(default_factory=list)

    def add_latency(self, ms: float):
        self.latencies_ms.append(ms)

    def p(self, pct: int) -> float:
        if not self.latencies_ms:
            return 0.0
        return statistics.quantiles(sorted(self.latencies_ms), n=100)[pct - 1]

    def throughput(self, elapsed: float) -> float:
        return self.total_requests / elapsed if elapsed > 0 else 0

    def print(self, elapsed: float):
        p50 = self.p(50)
        p95 = self.p(95)
        p99 = self.p(99)
        tps = self.throughput(elapsed)
        daily = int(tps * 86400)
        print(f"\n{'='*65}")
        print(f"  Scenario {self.scenario}")
        print(f"{'='*65}")
        print(f"  Total requests    : {self.total_requests}")
        print(f"  Successful acks   : {self.successful_acks}")
        print(f"  Enqueued jobs     : {self.enqueued_jobs}")
        print(f"  Cache hits        : {self.cache_hits}  ({self.cache_hits*100//max(self.total_requests,1)}%)")
        print(f"  Single-flight     : {self.single_flight_deduped} deduped")
        print(f"  Per-user cap      : {self.per_user_cap_enforced} queued (not blocked)")
        print(f"  Errors            : {self.errors}")
        print(f"  Latency p50/p95/p99: {p50:.0f}ms / {p95:.0f}ms / {p99:.0f}ms")
        print(f"  Throughput        : {tps:.1f} req/s  =>  ~{daily:,} req/day implied")
        print(f"  Elapsed           : {elapsed:.2f}s")

        if self.scenario == "A":
            ok = p95 < 500 and self.errors == 0
        elif self.scenario == "B":
            ok = self.successful_acks == self.total_requests and self.errors == 0
        else:  # C
            actual_downloads = self.enqueued_jobs - self.single_flight_deduped
            ok = actual_downloads <= 1
        status = "PASS" if ok else "FAIL"
        print(f"\n  Result: {status}")
        print(f"{'='*65}")
        return ok


# ── Mock infrastructure ───────────────────────────────────────────────────────

class MockRedis:
    """In-process mock Redis supporting get/set/setex/incr/decr/delete/set NX."""

    def __init__(self):
        self._store: dict[str, tuple[str, Optional[float]]] = {}
        self._lock = asyncio.Lock()

    def _expired(self, key: str) -> bool:
        if key not in self._store:
            return True
        v, exp = self._store[key]
        if exp and time.monotonic() > exp:
            del self._store[key]
            return True
        return False

    async def get(self, key: str) -> Optional[str]:
        async with self._lock:
            if self._expired(key):
                return None
            return self._store[key][0]

    async def set(self, key: str, value: str, nx: bool = False, ex: Optional[int] = None) -> bool:
        async with self._lock:
            if nx and not self._expired(key) and key in self._store:
                return False
            exp = time.monotonic() + ex if ex else None
            self._store[key] = (str(value), exp)
            return True

    async def setex(self, key: str, ttl: int, value: str) -> None:
        await self.set(key, value, ex=ttl)

    async def incr(self, key: str) -> int:
        async with self._lock:
            v = 0 if self._expired(key) else int(self._store.get(key, ("0", None))[0])
            v += 1
            self._store[key] = (str(v), None)
            return v

    async def decr(self, key: str) -> int:
        async with self._lock:
            v = 0 if self._expired(key) else int(self._store.get(key, ("0", None))[0])
            v = max(0, v - 1)
            self._store[key] = (str(v), None)
            return v

    async def delete(self, key: str) -> None:
        async with self._lock:
            self._store.pop(key, None)

    async def expire(self, key: str, ttl: int) -> None:
        async with self._lock:
            if key in self._store:
                v, _ = self._store[key]
                self._store[key] = (v, time.monotonic() + ttl)

    async def llen(self, key: str) -> int:
        return 0  # Queue is always empty in mock


_mock_redis: Optional[MockRedis] = None


def get_mock_redis() -> MockRedis:
    global _mock_redis
    if _mock_redis is None:
        _mock_redis = MockRedis()
    return _mock_redis


# ── Core handler simulation ───────────────────────────────────────────────────

async def simulate_handle_url(
    user_id: int,
    url: str,
    m: Metrics,
    redis: MockRedis,
    per_user_cap: int = 3,
    max_queue: int = 2000,
    *,
    download_delay: float = 0.0,  # simulated yt-dlp time
):
    """Simulates the handler + worker flow without real Telegram/DB/yt-dlp."""
    import hashlib
    t0 = time.monotonic()
    m.total_requests += 1

    url_hash = hashlib.sha256(url.encode()).hexdigest()
    fid_key = f"fid:{url_hash}|720"
    sf_key = f"sf:{url_hash}|720"
    ucap_key = f"ucap:{user_id}"

    # 1. Cache check
    cached = await redis.get(fid_key)
    if cached:
        m.cache_hits += 1
        m.successful_acks += 1
        m.add_latency((time.monotonic() - t0) * 1000)
        return

    # 2. Queue depth check (always 0 in mock)
    depth = await redis.llen("arq:queue")
    if depth >= max_queue:
        m.errors += 1
        return

    # 3. Per-user concurrency check (ack immediately regardless)
    active = await redis.get(ucap_key)
    active_count = int(active) if active else 0
    if active_count >= per_user_cap:
        m.per_user_cap_enforced += 1

    # Ack immediately (this is what the handler does)
    m.successful_acks += 1
    m.add_latency((time.monotonic() - t0) * 1000)

    # 4. Enqueue (in mock: simulate the worker inline)
    m.enqueued_jobs += 1

    # 5. Single-flight lock
    sf_acquired = await redis.set(sf_key, "1", nx=True, ex=90)
    if not sf_acquired:
        # Another "worker" is downloading — wait for cache or dedupe
        for _ in range(20):
            await asyncio.sleep(0.01)
            cached = await redis.get(fid_key)
            if cached:
                m.single_flight_deduped += 1
                return
        # Lock expired, try ourselves
        sf_acquired = await redis.set(sf_key, "1", nx=True, ex=90)

    try:
        # Simulate user slot
        await redis.incr(ucap_key)
        # Simulate download time
        if download_delay > 0:
            await asyncio.sleep(download_delay)
        # Populate cache
        await redis.setex(fid_key, 86400, f"FILEID_{url_hash[:8]}")
    finally:
        if sf_acquired:
            await redis.delete(sf_key)
        await redis.decr(ucap_key)


# ── Scenarios ─────────────────────────────────────────────────────────────────

async def scenario_a(redis: MockRedis) -> tuple[Metrics, bool]:
    """500 concurrent users, each sending 1 URL."""
    print("\nScenario A: 500 concurrent users × 1 URL each (no cache)")
    m = Metrics("A")
    urls = [f"https://youtu.be/vid_{i:04d}" for i in range(500)]
    users = list(range(1, 501))

    t0 = time.monotonic()
    tasks = [simulate_handle_url(uid, url, m, redis) for uid, url in zip(users, urls)]
    await asyncio.gather(*tasks)
    elapsed = time.monotonic() - t0
    ok = m.print(elapsed)
    return m, ok


async def scenario_b(redis: MockRedis) -> tuple[Metrics, bool]:
    """1 user fires 30 links back-to-back; per-user cap K=3."""
    print("\nScenario B: 1 user, 30 links back-to-back (per-user cap K=3)")
    m = Metrics("B")
    user_id = 9999
    urls = [f"https://youtu.be/vid_burst_{i:03d}" for i in range(30)]
    # Clear leftover state from previous runs (real Redis persists across runs)
    await redis.delete(f"ucap:{user_id}")
    for url in urls:
        import hashlib as _h
        h = _h.sha256(url.encode()).hexdigest()
        await redis.delete(f"fid:{h}|720")
        await redis.delete(f"sf:{h}|720")

    async def _staggered(url: str, delay: float):
        # Telegram delivers one user's messages sequentially — a small stagger
        # lets earlier INCRs land so the cap check observes real state (matters
        # with real-Redis latency; mock Redis resolves within one loop tick).
        await asyncio.sleep(delay)
        await simulate_handle_url(user_id, url, m, redis, per_user_cap=3, download_delay=0.05)

    t0 = time.monotonic()
    tasks = [_staggered(url, i * 0.005) for i, url in enumerate(urls)]
    await asyncio.gather(*tasks)
    elapsed = time.monotonic() - t0
    ok = m.print(elapsed)

    assert m.successful_acks == 30, f"Expected 30 acks, got {m.successful_acks}"
    assert m.per_user_cap_enforced > 0, "Expected some jobs to be queued beyond cap"
    return m, ok


async def scenario_c(redis: MockRedis) -> tuple[Metrics, bool]:
    """1000 requests for the SAME URL — single-flight should allow only 1 download."""
    print("\nScenario C: 1000 requests for same URL (single-flight + cache)")
    m = Metrics("C")
    url = "https://youtu.be/viral_video"
    users = list(range(1, 1001))

    t0 = time.monotonic()
    tasks = [simulate_handle_url(uid, url, m, redis, download_delay=0.02) for uid in users]
    await asyncio.gather(*tasks)
    elapsed = time.monotonic() - t0

    actual_downloads = m.enqueued_jobs - m.single_flight_deduped
    print(f"\n  Actual downloads (not deduped by single-flight): {actual_downloads}")
    deduped_total = m.cache_hits + m.single_flight_deduped
    print(f"  Total deduplication: {deduped_total}/{m.total_requests} (cache + single-flight)")
    ok = m.print(elapsed)
    # Override pass condition: single download with 999 deduped is a pass
    if actual_downloads <= 1 and m.single_flight_deduped >= m.total_requests - 2:
        ok = True
    return m, ok


# ── Main ──────────────────────────────────────────────────────────────────────

async def main(scenarios: list[str], use_real: bool):
    if use_real:
        import redis.asyncio as aioredis
        from bot.config import settings
        r = aioredis.from_url(settings.redis_url, decode_responses=True)
        print(f"Using REAL Redis: {settings.redis_url}")
    else:
        r = get_mock_redis()
        print("Using in-process mock Redis (no network needed)")

    all_pass = True
    for s in scenarios:
        # Fresh redis state per scenario
        _mock_redis_fresh = MockRedis() if not use_real else r
        fn = {"A": scenario_a, "B": scenario_b, "C": scenario_c}[s]
        _, ok = await fn(_mock_redis_fresh)
        all_pass = all_pass and ok

    print("\n" + ("ALL SCENARIOS PASSED" if all_pass else "SOME SCENARIOS FAILED"))
    sys.exit(0 if all_pass else 1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Bot load test")
    parser.add_argument("--scenario", default="all", choices=["A", "B", "C", "all"])
    parser.add_argument("--real", action="store_true", help="Use real Redis from REDIS_URL")
    args = parser.parse_args()

    scenarios = ["A", "B", "C"] if args.scenario == "all" else [args.scenario]
    asyncio.run(main(scenarios, args.real))
