#!/usr/bin/env python3
"""
Startup healthcheck for @vidyuklaydi_bot.

Run: python -m bot.healthcheck
Exits 0 if all required checks pass (warnings allowed).
Exits 1 if any required check fails.

Each check prints: [OK] / [WARN] / [FAIL]  <service>: <detail>
"""
from __future__ import annotations

import asyncio
import shutil
import sys

# ── Colour helpers ─────────────────────────────────────────────────────────────
_OK   = "\033[32m[OK  ]\033[0m"
_WARN = "\033[33m[WARN]\033[0m"
_FAIL = "\033[31m[FAIL]\033[0m"

_failures: list[str] = []
_warnings: list[str] = []


def ok(label: str, detail: str = "") -> None:
    print(f"  {_OK}  {label}" + (f": {detail}" if detail else ""))


def warn(label: str, detail: str = "") -> None:
    _warnings.append(label)
    print(f"  {_WARN}  {label}" + (f": {detail}" if detail else ""))


def fail(label: str, detail: str = "") -> None:
    _failures.append(label)
    print(f"  {_FAIL}  {label}" + (f": {detail}" if detail else ""))


# ── Check functions ────────────────────────────────────────────────────────────

async def check_env_vars() -> None:
    print("\n── 1. Environment variables ──────────────────────────────────────")
    from bot.config import settings

    required = {
        "BOT_TOKEN":           bool(settings.bot_token and settings.bot_token != "0:dummy"),
        "TELEGRAM_API_ID":     bool(settings.telegram_api_id),
        "TELEGRAM_API_HASH":   bool(settings.telegram_api_hash and len(settings.telegram_api_hash) > 8),
        "DATABASE_URL":        bool(settings.database_url),
        "REDIS_URL":           bool(settings.redis_url),
        "ADMIN_IDS":           bool(settings.admin_ids),
        "DOWNLOAD_DIR":        bool(settings.download_dir),
    }
    optional = {
        "LOCAL_API_URL":       (bool(settings.local_api_url), "Local Bot API (50 MB limit without this)"),
        "GENIUS_TOKEN":        (bool(settings.genius_token), "Lyrics lookup may fail"),
    }

    for name, present in required.items():
        if present:
            ok(name)
        else:
            fail(name, "Missing or default value — MUST be set before go-live")

    for name, (present, note) in optional.items():
        if present:
            ok(name)
        else:
            warn(name, f"Not set — {note}")


async def check_redis() -> None:
    print("\n── 2. Redis ──────────────────────────────────────────────────────")
    try:
        from bot.services.cache import get_redis, ping_redis
        r = await get_redis()
        pong = await ping_redis()
        if pong:
            info = await r.info("server")
            ver = info.get("redis_version", "?")
            maxmem = info.get("maxmemory", 0)
            policy = info.get("maxmemory_policy", "none")
            ok("Redis reachable", f"version={ver}")
            if maxmem and maxmem > 0:
                ok("Redis maxmemory", f"{maxmem // 1024 // 1024}MB policy={policy}")
            else:
                warn("Redis maxmemory", "Not configured — set --maxmemory for production")
        else:
            fail("Redis", "ping failed")
    except Exception as e:
        fail("Redis", str(e)[:80])


async def check_postgres() -> None:
    print("\n── 3. Postgres ───────────────────────────────────────────────────")
    try:
        from bot.db.session import engine
        from sqlalchemy import text
        async with engine.connect() as conn:
            row = await conn.execute(text("SELECT current_database(), version()"))
            fetched = row.fetchone()
            if fetched is None:
                raise RuntimeError("SELECT returned no row")
            db_name, ver = fetched
            ok("Postgres reachable", f"db={db_name}")
            ok("Postgres version", ver.split(",")[0])

        # Check tables exist
        async with engine.connect() as conn:
            r = await conn.execute(text(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_schema='public'"
            ))
            n = r.scalar()
            if n and n >= 7:
                ok("Postgres tables", f"{n} tables in public schema")
            elif n and n > 0:
                warn("Postgres tables", f"Only {n} tables — may need migration")
            else:
                warn("Postgres tables", "No tables found — run migrations")
    except Exception as e:
        fail("Postgres", str(e)[:100])


async def check_pgbouncer() -> None:
    print("\n── 4. PgBouncer ──────────────────────────────────────────────────")
    from bot.config import settings
    db_url = settings.database_url
    # PgBouncer is only relevant in Docker (host=pgbouncer)
    if "pgbouncer" in db_url:
        try:
            from bot.db.session import engine
            from sqlalchemy import text
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            ok("PgBouncer", "connection through pgbouncer succeeded")
        except Exception as e:
            fail("PgBouncer", str(e)[:80])
    else:
        warn("PgBouncer", "DATABASE_URL doesn't route through pgbouncer (OK for dev; use in prod)")


async def check_binaries() -> None:
    print("\n── 5. System binaries ────────────────────────────────────────────")
    # ffmpeg
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        ok("ffmpeg", ffmpeg)
    else:
        # Try common Windows paths
        import os
        for d in [r"C:\ffmpeg\bin", r"C:\Program Files\ffmpeg\bin"]:
            candidate = os.path.join(d, "ffmpeg.exe")
            if os.path.exists(candidate):
                ffmpeg = candidate
                break
        if ffmpeg:
            ok("ffmpeg", ffmpeg)
        else:
            fail("ffmpeg", "Not found in PATH — required for audio extraction and muxing")

    # aria2c
    aria2c = shutil.which("aria2c")
    if aria2c:
        ok("aria2c", aria2c)
    else:
        warn("aria2c", "Not found — downloads will use yt-dlp's built-in HTTP (slower); install in Docker")

    # node (for YouTube EJS solver)
    node = shutil.which("node")
    if node:
        import subprocess
        try:
            ver = subprocess.check_output([node, "--version"], timeout=5).decode().strip()
            ok("node.js", f"{ver} at {node} — YouTube n-challenge solver available")
        except Exception:
            ok("node.js", node)
    else:
        warn("node.js", "Not found — YouTube n-challenge may fail (add nodejs to Dockerfile)")


async def check_yt_dlp() -> None:
    print("\n── 6. yt-dlp ─────────────────────────────────────────────────────")
    try:
        import yt_dlp
        try:
            from yt_dlp.version import __version__ as ver
        except Exception:
            ver = getattr(yt_dlp, "__version__", "unknown")
        ok("yt-dlp importable", f"version={ver}")
        # Check version is reasonably recent (>= 2026)
        if ver.startswith("20") and int(ver[:4]) < 2026:
            warn("yt-dlp version", f"{ver} is old — YouTube may throttle; run: pip install --upgrade yt-dlp")
        else:
            ok("yt-dlp version", f"{ver} (recent)")
    except ImportError as e:
        fail("yt-dlp", f"Cannot import: {e}")


async def check_telegram() -> None:
    print("\n── 7. Telegram Bot API ───────────────────────────────────────────")
    from bot.config import settings
    if not settings.bot_token or settings.bot_token.startswith("0:"):
        warn("Telegram getMe", "BOT_TOKEN is dummy — skipping live check")
        return
    try:
        import aiohttp
        api_base = settings.local_api_url.rstrip("/") if settings.use_local_api else "https://api.telegram.org"
        url = f"{api_base}/bot{settings.bot_token}/getMe"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                data = await resp.json()
                if data.get("ok"):
                    bot_info = data["result"]
                    ok("Telegram getMe", f"@{bot_info.get('username')} (id={bot_info.get('id')})")
                    if settings.use_local_api:
                        ok("Local Bot API", f"Using {settings.local_api_url} — 2GB file limit active")
                    else:
                        warn("Telegram API", "Using cloud API — 50MB file limit. Set LOCAL_API_URL for production.")
                else:
                    fail("Telegram getMe", data.get("description", "unknown error"))
    except Exception as e:
        fail("Telegram getMe", str(e)[:100])


async def check_download_dir() -> None:
    print("\n── 8. Download directory ─────────────────────────────────────────")
    import os
    from bot.config import settings
    d = settings.download_dir
    try:
        os.makedirs(d, exist_ok=True)
        test_file = os.path.join(d, ".hc_probe")
        with open(test_file, "w") as f:
            f.write("ok")
        os.unlink(test_file)
        ok("DOWNLOAD_DIR", f"{d} — writable")
    except Exception as e:
        fail("DOWNLOAD_DIR", f"{d}: {e}")


# ── Entry point ────────────────────────────────────────────────────────────────

async def run_all() -> int:
    print("=" * 65)
    print("  @vidyuklaydi_bot — Startup Healthcheck")
    print("=" * 65)

    await check_env_vars()
    await check_redis()
    await check_postgres()
    await check_pgbouncer()
    await check_binaries()
    await check_yt_dlp()
    await check_telegram()
    await check_download_dir()

    print("\n" + "=" * 65)
    if _failures:
        print(f"  {_FAIL}  {len(_failures)} FAILED: {', '.join(_failures)}")
        print("  Bot should NOT start with these failures.")
        print("=" * 65)
        return 1

    if _warnings:
        print(f"  {_WARN}  {len(_warnings)} warnings: {', '.join(_warnings)}")
        print("  Bot CAN start but review warnings before go-live.")
    else:
        print(f"  {_OK}  All checks passed — ready to start.")
    print("=" * 65)
    return 0


def main():
    exit_code = asyncio.run(run_all())
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
