#!/usr/bin/env python3
"""One-time login of YOUR OWN Telegram account for the recon tools.

Run this in your own terminal (it asks for your phone number and the code
Telegram sends you; it never stores the code):

    <python-with-telethon> replica/tools/tg_login.py

The session is saved to secrets/tg_user.session (gitignored). Delete that file
to log the recon tools out.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SESSION = ROOT / "secrets" / "tg_user"


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def main() -> int:
    from telethon import TelegramClient

    env = load_env()
    api_id = int(env["TELEGRAM_API_ID"])
    api_hash = env["TELEGRAM_API_HASH"]
    (ROOT / "secrets").mkdir(exist_ok=True)
    client = TelegramClient(str(SESSION), api_id, api_hash)

    async def run() -> None:
        await client.start()  # interactive: phone -> code -> (2FA password if set)
        me = await client.get_me()
        print(f"Logged in as {me.first_name} (id {me.id}). Session: {SESSION}.session")

    with client:
        client.loop.run_until_complete(run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
