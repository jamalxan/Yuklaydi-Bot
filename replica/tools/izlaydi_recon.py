#!/usr/bin/env python3
"""Recon driver: talks to a Telegram bot from YOUR OWN account and records every
reply — text, inline/reply keyboards, media attributes, message edits and
deletions, chat actions ("uploading video...") — with second-precision timing.

Reading, not scraping: one probe at a time, human-paced, your own account.

Usage:
    python replica/tools/izlaydi_recon.py --bot IzlaydiBot --probe youtube
    python replica/tools/izlaydi_recon.py --bot IzlaydiBot --probe all
    python replica/tools/izlaydi_recon.py --bot IzlaydiBot --probe youtube --click "720"
    python replica/tools/izlaydi_recon.py --bot IzlaydiBot --text "any message"
    python replica/tools/izlaydi_recon.py --bot IzlaydiBot --probe youtube --download

Appends JSON lines to replica/<bot>_log.jsonl and prints a transcript.
Requires: secrets/tg_user.session (run replica/tools/tg_login.py once).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SESSION = ROOT / "secrets" / "tg_user"
FIX = ROOT / "tests" / "fixtures"

PROBES: dict[str, list[tuple[str, str]]] = {
    "start": [("text", "/start")],
    "help": [("text", "/help")],
    "youtube": [("text", "https://www.youtube.com/watch?v=jNQXAC9IVRw")],           # 19 s
    "youtube_long": [("text", "https://www.youtube.com/watch?v=aqz-KE-bpKQ")],      # 10:34, ~150 MB @720p
    "youtube_short": [("text", "https://www.youtube.com/shorts/tPEE9ZwTmy0")],      # a Short
    "instagram": [("text", "https://www.instagram.com/reel/Chunk8-jurw/")],
    "tiktok": [("text", "https://www.tiktok.com/@tiktok/video/7231338487075638570")],
    "music_text": [("text", "Botir Qodirov Oyning o'n beshi qorong'u")],
    "lyrics_text": [("text", "oyning o'n beshi qorong'u")],
    "voice": [("voice", str(FIX / "voice_11s.ogg"))],
    "video_note": [("video_note", str(FIX / "video_note_14s.mp4"))],
    "unknown_link": [("text", "https://example.com/some/page")],
    "garbage": [("text", "asdkjh qwe 123")],
}


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def describe(m) -> dict:
    d: dict = {
        "id": m.id,
        "date": m.date.isoformat(),
        "edit_date": m.edit_date.isoformat() if m.edit_date else None,
        "out": bool(m.out),
        "text": m.message or "",
    }
    if m.entities:
        d["entities"] = [
            {"type": e.__class__.__name__.replace("MessageEntity", ""), "url": getattr(e, "url", None)}
            for e in m.entities
        ]
    media = m.media
    if media is not None:
        d["media"] = media.__class__.__name__.replace("MessageMedia", "")
        doc = getattr(media, "document", None)
        if doc is not None:
            d["mime"] = doc.mime_type
            d["size"] = doc.size
            d["has_thumb"] = bool(doc.thumbs)
            for a in doc.attributes:
                n = a.__class__.__name__
                if n == "DocumentAttributeVideo":
                    d.update(duration=a.duration, w=a.w, h=a.h, round=bool(a.round_message),
                             streaming=bool(a.supports_streaming))
                elif n == "DocumentAttributeAudio":
                    d.update(duration=a.duration, voice=bool(a.voice), title=a.title, performer=a.performer)
                elif n == "DocumentAttributeFilename":
                    d["filename"] = a.file_name
        if getattr(media, "photo", None) is not None:
            d["photo"] = True
        if getattr(media, "webpage", None) is not None:
            d["webpage"] = True
    rm = m.reply_markup
    if rm is not None:
        rows = []
        for row in getattr(rm, "rows", []) or []:
            r = []
            for b in row.buttons:
                bd = {"text": b.text, "type": b.__class__.__name__.replace("KeyboardButton", "") or "Button"}
                if getattr(b, "url", None):
                    bd["url"] = b.url
                data = getattr(b, "data", None)
                if data is not None:
                    bd["data"] = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else str(data)
                if getattr(b, "query", None) is not None:
                    bd["query"] = b.query
                r.append(bd)
            rows.append(r)
        d["keyboard"] = {
            "type": rm.__class__.__name__,
            "rows": rows,
            "resize": getattr(rm, "resize", None),
            "one_time": getattr(rm, "single_use", None),
            "placeholder": getattr(rm, "placeholder", None),
        }
    return d


def fmt_kb(kb: dict | None) -> str:
    if not kb:
        return ""
    lines = []
    for row in kb["rows"]:
        cells = []
        for b in row:
            extra = ""
            if b.get("url"):
                extra = f" -> {b['url']}"
            elif b.get("data"):
                extra = f" [{b['data']}]"
            cells.append(f"[{b['text']}{extra}]")
        lines.append(" ".join(cells))
    return f"  keyboard({kb['type']}):\n    " + "\n    ".join(lines)


def fmt_event(ev: dict) -> str:
    t = f"+{ev['dt']:6.1f}s"
    kind = ev["kind"]
    m = ev.get("msg") or {}
    if kind == "action":
        return f"{t}  action: {ev['action']}"
    if kind == "deleted":
        return f"{t}  DELETED msg {ev['id']}"
    head = f"{t}  {kind.upper()} msg {m.get('id')}"
    body = []
    if m.get("media"):
        meta = {k: m[k] for k in ("mime", "size", "duration", "w", "h", "streaming", "voice", "title",
                                  "performer", "filename", "has_thumb", "round", "photo", "webpage") if k in m}
        body.append(f"  media={m['media']} {meta}")
    if m.get("text"):
        body.append("  text: " + m["text"].replace("\n", "\n        "))
    kb = fmt_kb(m.get("keyboard"))
    if kb:
        body.append(kb)
    return head + ("\n" + "\n".join(body) if body else "")


async def send_step(client, bot, kind: str, payload: str):
    if kind == "text":
        return await client.send_message(bot, payload, link_preview=False)
    if kind == "voice":
        return await client.send_file(bot, payload, voice_note=True)
    if kind == "video_note":
        return await client.send_file(bot, payload, video_note=True)
    if kind == "file":
        return await client.send_file(bot, payload)
    raise ValueError(kind)


async def observe(client, bot, t0: float, actions: list, settle: float, max_wait: float,
                  known: dict[int, dict], first_id: int, download: bool, media_dir: Path) -> list[dict]:
    """Poll the chat until the bot has been quiet for `settle` seconds."""
    events: list[dict] = []
    last_change = time.time()
    got_any = False
    seen_actions = 0
    while True:
        now = time.time()
        # chat actions captured by the event handler
        while seen_actions < len(actions):
            a = actions[seen_actions]
            seen_actions += 1
            events.append({"kind": "action", "dt": round(a[0] - t0, 2), "action": a[1]})
            last_change = now
        msgs = await client.get_messages(bot, limit=25)
        current_ids = set()
        for m in sorted(msgs, key=lambda x: x.id):
            if m.id < first_id:
                continue
            current_ids.add(m.id)
            if m.out:
                continue
            d = describe(m)
            prev = known.get(m.id)
            if prev is None:
                known[m.id] = d
                events.append({"kind": "new", "dt": round(now - t0, 2), "msg": d})
                last_change = now
                got_any = True
                if download and d.get("media") in ("Document", "Photo"):
                    try:
                        media_dir.mkdir(parents=True, exist_ok=True)
                        path = await client.download_media(m, file=str(media_dir / f"{bot}_{m.id}"))
                        events.append({"kind": "downloaded", "dt": round(time.time() - t0, 2), "path": str(path)})
                    except Exception as e:  # noqa: BLE001
                        events.append({"kind": "download_error", "dt": round(time.time() - t0, 2), "error": str(e)})
            elif (prev.get("text"), prev.get("edit_date"), json.dumps(prev.get("keyboard"), sort_keys=True),
                  prev.get("media")) != (d.get("text"), d.get("edit_date"),
                                         json.dumps(d.get("keyboard"), sort_keys=True), d.get("media")):
                known[m.id] = d
                events.append({"kind": "edit", "dt": round(now - t0, 2), "msg": d})
                last_change = now
        for mid in list(known):
            if mid >= first_id and mid not in current_ids and not known[mid].get("_deleted"):
                known[mid]["_deleted"] = True
                events.append({"kind": "deleted", "dt": round(now - t0, 2), "id": mid})
                last_change = now
        if got_any and now - last_change >= settle:
            break
        if now - t0 >= max_wait:
            events.append({"kind": "timeout", "dt": round(now - t0, 2)})
            break
        await asyncio.sleep(1.0)
    return events


async def run(args) -> int:
    from telethon import TelegramClient, events as tev

    env = load_env()
    client = TelegramClient(str(SESSION), int(env["TELEGRAM_API_ID"]), env["TELEGRAM_API_HASH"])
    await client.connect()
    if not await client.is_user_authorized():
        print("Not logged in. Run replica/tools/tg_login.py first.", file=sys.stderr)
        return 2
    bot = await client.get_entity(args.bot)
    actions: list[tuple[float, str]] = []

    @client.on(tev.UserUpdate(chats=bot))
    async def _on_action(ev):  # typing / upload_video / record_audio ...
        name = ev.action.__class__.__name__.replace("SendMessage", "").replace("Action", "") if ev.action else "?"
        actions.append((time.time(), name))

    log_path = ROOT / "replica" / f"{args.bot}_log.jsonl"
    media_dir = ROOT / "replica" / "screens" / f"{args.bot}_media"

    if args.text:
        plan = [("custom", [("text", args.text)])]
    elif args.file:
        plan = [("custom_file", [("file", args.file)])]
    elif args.probe == "all":
        plan = list(PROBES.items())
    else:
        plan = [(p, PROBES[p]) for p in args.probe.split(",")]

    for name, steps in plan:
        for kind, payload in steps:
            known: dict[int, dict] = {}
            latest = await client.get_messages(bot, limit=1)
            first_id = (latest[0].id + 1) if latest else 0
            actions.clear()
            t0 = time.time()
            sent = await send_step(client, bot, kind, payload)
            first_id = min(first_id, sent.id)
            print(f"\n=== probe {name}: {kind} {payload[:80]}  (sent msg {sent.id} at {datetime.now(timezone.utc).isoformat()})")
            evs = await observe(client, bot, t0, actions, args.settle, args.max_wait, known, first_id,
                                args.download, media_dir)
            for ev in evs:
                print(fmt_event(ev))
            record = {"bot": args.bot, "probe": name, "kind": kind, "payload": payload,
                      "sent_at": datetime.fromtimestamp(t0, timezone.utc).isoformat(), "events": evs}
            if args.click:
                # press an inline button on the newest bot message that has it
                target = None
                for mid in sorted(known, reverse=True):
                    kb = known[mid].get("keyboard") or {}
                    for row in kb.get("rows", []):
                        for b in row:
                            if args.click.lower() in b["text"].lower() and b.get("data") is not None:
                                target = (mid, b["text"])
                                break
                        if target:
                            break
                    if target:
                        break
                if target:
                    msg = await client.get_messages(bot, ids=target[0])
                    t1 = time.time()
                    actions.clear()
                    print(f"\n--- click [{target[1]}] on msg {target[0]}")
                    try:
                        await msg.click(text=target[1])
                    except Exception as e:  # noqa: BLE001
                        print(f"click error: {e}")
                    evs2 = await observe(client, bot, t1, actions, args.settle, args.max_wait, known, first_id,
                                         args.download, media_dir)
                    for ev in evs2:
                        print(fmt_event(ev))
                    record["click"] = {"button": target[1], "events": evs2}
                else:
                    print(f"--- no inline button matching {args.click!r} found")
            with log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            await asyncio.sleep(args.pause)
    await client.disconnect()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bot", default="IzlaydiBot")
    ap.add_argument("--probe", default="start", help="comma list of probe names, or 'all'")
    ap.add_argument("--text", help="send this exact text instead of a probe")
    ap.add_argument("--file", help="send this file instead of a probe")
    ap.add_argument("--click", help="after the probe, press the inline button containing this text")
    ap.add_argument("--settle", type=float, default=10.0, help="seconds of silence that end a probe")
    ap.add_argument("--max-wait", type=float, default=300.0)
    ap.add_argument("--pause", type=float, default=3.0, help="seconds between probes")
    ap.add_argument("--download", action="store_true", help="download replied media for inspection")
    args = ap.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
