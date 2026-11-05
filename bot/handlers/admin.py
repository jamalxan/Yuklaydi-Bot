from __future__ import annotations

import csv
import secrets
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlsplit

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.config import settings
from bot.db.repo import SettingRepo, StatsRepo, UserRepo, WelcomeRepo
from bot.handlers.start import _render
from bot.handlers.states import AdminFSM
from bot.keyboards.inline import (
    admin_back_keyboard, admin_ban_keyboard, admin_broadcast_confirm_keyboard,
    admin_broadcast_skip_keyboard, admin_channel_keyboard,
    admin_main_keyboard, admin_welcome_confirm_keyboard,
    admin_welcome_lang_keyboard,
)
from bot.logger import logger
from bot.services.broadcast import RATE_PER_SECOND
from bot.texts import t
from bot.utils.text import esc

router = Router()
router.message.filter(F.chat.type == ChatType.PRIVATE)

_BROADCAST_RATE = RATE_PER_SECOND  # messages per second (kept for imports/tests)
_CTA_TEXT_MAX = 64
_PANEL = "🛠 <b>Admin Panel</b>"
_LANG_NAMES = {"uz": "O'zbek", "oz": "Ўзбекча", "ru": "Русский", "en": "English"}


def _is_admin(user_id: int) -> bool:
    return user_id in settings.admin_ids


def _admin_filter(event) -> bool:
    u = getattr(event, "from_user", None)
    return bool(u and _is_admin(u.id))


# Every admin handler below is gated by the router-level filter, so the
# per-handler "if not admin: return" checks are gone.
router.message.filter(_admin_filter)
router.callback_query.filter(_admin_filter)


def _session():
    from bot.db.session import async_session_factory
    return async_session_factory()


# ── Commands ──────────────────────────────────────────────────────────────────

@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(_PANEL, parse_mode="HTML", reply_markup=admin_main_keyboard())


@router.message(Command("statistics"))
async def cmd_statistics(message: Message):
    await message.answer(await _build_stats(), parse_mode="HTML")


@router.callback_query(F.data == "adm:main")
async def cb_adm_main(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await callback.message.edit_text(_PANEL, parse_mode="HTML", reply_markup=admin_main_keyboard())
    except TelegramBadRequest:
        await callback.message.answer(_PANEL, parse_mode="HTML", reply_markup=admin_main_keyboard())
    await callback.answer()


@router.callback_query(F.data == "adm:close")
async def cb_adm_close(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await callback.message.delete()
    except TelegramBadRequest:
        pass
    await callback.answer()


# ── Statistics ────────────────────────────────────────────────────────────────

async def _build_stats() -> str:
    try:
        async with _session() as session:
            s = await StatsRepo(session).snapshot()
    except Exception as e:
        logger.error(f"Stats error: {e}")
        return "❌ Statistika mavjud emas."

    top_lines = "\n".join(
        f"  {i + 1}. {esc(row[0] or 'unknown')}: {row[1]}" for i, row in enumerate(s.top_platforms)
    ) or "  —"
    cache_rate = f"{s.cache_today}/{s.downloads_today}"

    avg_t = "—"
    try:
        from bot.services.cache import get_redis
        r = await get_redis()
        t_sum_str = await r.get("stat:t_total_sum")
        t_cnt_str = await r.get("stat:t_total_count")
        if t_sum_str and t_cnt_str and int(t_cnt_str) > 0:
            avg_t = f"{float(t_sum_str) / int(t_cnt_str):.1f}s (n={int(t_cnt_str)})"
    except Exception:
        pass

    return (
        "📊 <b>Statistika</b>\n"
        f"<i>Kun chegarasi: {esc(settings.stats_tz)}</i>\n\n"
        f"👥 <b>Foydalanuvchilar:</b>\n"
        f"  Jami: <b>{s.users_total}</b> | Bugun: <b>{s.users_today}</b> | 7 kun: <b>{s.users_week}</b>\n"
        f"  Faol 7 kun: <b>{s.active_7}</b> | Faol 30 kun: <b>{s.active_30}</b>\n\n"
        f"🎬 <b>Yuklamalar:</b> jami <b>{s.downloads_total}</b> | bugun <b>{s.downloads_today}</b>"
        f" (shundan musiqa: {s.music_today})\n"
        f"🎵 <b>Aniqlashlar:</b> jami <b>{s.recognitions_total}</b> | bugun <b>{s.recognitions_today}</b>\n"
        f"🔍 <b>Qidiruvlar:</b> jami <b>{s.searches_total}</b> | bugun <b>{s.searches_today}</b>\n\n"
        f"🏆 <b>Top platformalar:</b>\n{top_lines}\n\n"
        f"⚡ <b>Kesh (bugun):</b> {cache_rate}\n"
        f"⏱ <b>O'rtacha vaqt:</b> {avg_t}"
    )


@router.callback_query(F.data == "adm:stats")
async def cb_adm_stats(callback: CallbackQuery):
    await callback.answer("⏳")
    text = await _build_stats()
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=admin_back_keyboard())
    except TelegramBadRequest:
        await callback.message.answer(text, parse_mode="HTML", reply_markup=admin_back_keyboard())


# ── Broadcast ─────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "adm:broadcast")
async def cb_adm_broadcast(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminFSM.broadcast_content)
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="❌ Bekor qilish", callback_data="adm:main"))
    await callback.message.edit_text(
        "📢 <b>Broadcast</b>\n\nXabar yuboring (matn, rasm yoki video). Caption ham qo'shishingiz mumkin.",
        parse_mode="HTML", reply_markup=builder.as_markup(),
    )
    await callback.answer()


@router.message(AdminFSM.broadcast_content)
async def fsm_broadcast_content(message: Message, state: FSMContext):
    await state.update_data(broadcast={
        "chat_id": message.chat.id,
        "message_id": message.message_id,
        "cta_text": None,
        "cta_url": None,
    })
    await state.set_state(AdminFSM.broadcast_button)
    await message.answer(
        "🔘 CTA tugmasi qo'shish? <code>Matn - https://url</code> formatida yuboring yoki o'tkazib yuboring.",
        parse_mode="HTML",
        reply_markup=admin_broadcast_skip_keyboard(),
    )


@router.callback_query(F.data == "adm:bc_skip_btn", AdminFSM.broadcast_button)
async def fsm_bc_skip_btn(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await _show_broadcast_preview(callback.message, state, callback.bot)


def parse_cta(text: str) -> Optional[tuple[str, str]]:
    """'Label - https://host/path' → (label, url); None if invalid."""
    if " - " not in text:
        return None
    label, url = (p.strip() for p in text.split(" - ", 1))
    if not label or len(label) > _CTA_TEXT_MAX:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme != "https" or not parts.hostname or " " in url:
        return None
    return label, url


@router.message(AdminFSM.broadcast_button)
async def fsm_broadcast_button(message: Message, state: FSMContext):
    cta = parse_cta((message.text or "").strip())
    if cta is None:
        await message.answer(
            f"❌ Noto'g'ri format. <code>Matn - https://url</code> yuboring "
            f"(matn ≤ {_CTA_TEXT_MAX} belgi, faqat https).",
            parse_mode="HTML",
        )
        return
    data = await state.get_data()
    bc = data.get("broadcast", {})
    bc["cta_text"], bc["cta_url"] = cta
    await state.update_data(broadcast=bc)
    await _show_broadcast_preview(message, state, message.bot)


def _cta_markup(bc: dict):
    if not (bc.get("cta_text") and bc.get("cta_url")):
        return None
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text=bc["cta_text"], url=bc["cta_url"]))
    return b.as_markup()


async def _show_broadcast_preview(target: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    bc = data.get("broadcast", {})

    recipient_count: int | str
    try:
        async with _session() as session:
            recipient_count = await UserRepo(session).count_broadcast_recipients()
    except Exception:
        recipient_count = "?"

    await target.answer(
        f"👁 <b>Ko'rinishi (preview):</b>\n📤 Qabul qiluvchilar: <b>{recipient_count}</b>\n\n",
        parse_mode="HTML",
    )
    try:
        await bot.copy_message(
            chat_id=target.chat.id,
            from_chat_id=bc["chat_id"],
            message_id=bc["message_id"],
            reply_markup=_cta_markup(bc),
        )
    except Exception as e:
        await target.answer(f"❌ Preview xatosi: {esc(e)}", parse_mode="HTML")
        return

    await state.update_data(recipient_count=recipient_count if isinstance(recipient_count, int) else 0)
    await state.set_state(AdminFSM.broadcast_confirm)
    await target.answer("✅ Yuborish uchun tasdiqlang:", reply_markup=admin_broadcast_confirm_keyboard())


@router.callback_query(F.data == "adm:bc_cancel")
async def fsm_bc_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌ Broadcast bekor qilindi.")
    await callback.answer()
    await callback.message.answer(_PANEL, parse_mode="HTML", reply_markup=admin_main_keyboard())


@router.callback_query(F.data == "adm:bc_send", AdminFSM.broadcast_confirm)
async def fsm_bc_send(callback: CallbackQuery, state: FSMContext):
    from bot.services.cache import acquire_broadcast_lock, release_broadcast_lock

    data = await state.get_data()
    bc = data.get("broadcast", {})
    await state.clear()
    if not bc.get("message_id"):
        await callback.answer("❌ Broadcast ma'lumoti topilmadi.", show_alert=True)
        return

    broadcast_id = secrets.token_hex(6)
    if not await acquire_broadcast_lock(broadcast_id):
        await callback.answer("⏳ Boshqa broadcast hali davom etmoqda.", show_alert=True)
        return

    await callback.answer()
    status = await callback.message.edit_text("📤 Broadcast navbatga qo'yildi...")
    try:
        from bot.services.queue import get_arq_pool
        pool = await get_arq_pool()
        await pool.enqueue_job(
            "broadcast_task",
            broadcast_id=broadcast_id,
            admin_chat_id=callback.message.chat.id,
            status_message_id=status.message_id if isinstance(status, Message) else callback.message.message_id,
            from_chat_id=bc["chat_id"],
            message_id=bc["message_id"],
            cta_text=bc.get("cta_text"),
            cta_url=bc.get("cta_url"),
            total=int(data.get("recipient_count") or 0),
            _job_id=f"broadcast:{broadcast_id}",
            _queue_name="arq:queue",
        )
    except Exception as e:
        logger.error(f"Broadcast enqueue failed: {e}")
        await release_broadcast_lock()
        await callback.message.answer(f"❌ Broadcast boshlanmadi: {esc(e)}", parse_mode="HTML",
                                      reply_markup=admin_back_keyboard())


# ── Edit welcome message ──────────────────────────────────────────────────────

@router.callback_query(F.data == "adm:welcome")
async def cb_adm_welcome(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminFSM.welcome_lang)
    await callback.message.edit_text(
        "✏️ <b>Xush kelibsiz xabarini tahrirlash</b>\n\nQaysi til uchun?",
        parse_mode="HTML", reply_markup=admin_welcome_lang_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data.in_({"adm:wl_uz", "adm:wl_oz", "adm:wl_ru", "adm:wl_en"}))
async def fsm_welcome_lang(callback: CallbackQuery, state: FSMContext):
    lang = callback.data.rsplit("_", 1)[-1]
    await state.update_data(welcome_lang=lang)
    await state.set_state(AdminFSM.welcome_new_text)

    try:
        async with _session() as session:
            current = await WelcomeRepo(session).get(lang) or "—"
    except Exception:
        current = "—"

    # Show the stored template as SOURCE (escaped) — it contains <b> etc.,
    # which Telegram rejects inside <code>, so the old screen often failed.
    text = (
        f"✏️ <b>{_LANG_NAMES[lang]}</b> uchun joriy xabar:\n\n"
        f"<code>{esc(current)}</code>\n\n"
        "Yangi xabarni yuboring — Telegram formatlashi (qalin, kursiv, havola) saqlanadi.\n"
        "O'rinbosarlar: <code>{first_name}</code>, <code>{username}</code>"
    )
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="❌ Bekor", callback_data="adm:welcome"))
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=builder.as_markup())
    await callback.answer()


@router.message(AdminFSM.welcome_new_text)
async def fsm_welcome_new_text(message: Message, state: FSMContext):
    # html_text converts the admin's Telegram formatting into valid HTML; a
    # plain .text dropped bold/italic and let hand-typed broken tags through.
    new_text = (message.html_text or "").strip() if (message.text or message.caption) else ""
    if not new_text:
        await message.answer("❌ Matn bo'sh bo'lmasligi kerak. Matnli xabar yuboring.")
        return

    preview = _render(new_text, "Ali", "ali")
    try:
        # The preview IS the validation: if Telegram can't parse it here it
        # would break /start for every user, so it is never offered for saving.
        await message.answer(
            f"👁 <b>Preview:</b>\n\n{preview}",
            parse_mode="HTML",
        )
    except TelegramBadRequest as e:
        await message.answer(
            f"❌ Xabar saqlanmadi — HTML xato:\n<code>{esc(e.message)}</code>\n\nQaytadan yuboring.",
            parse_mode="HTML",
        )
        return

    await state.update_data(welcome_new_text=new_text)
    await state.set_state(AdminFSM.welcome_confirm)
    await message.answer("<b>Saqlaysizmi?</b>", parse_mode="HTML", reply_markup=admin_welcome_confirm_keyboard())


@router.callback_query(F.data == "adm:wl_save", AdminFSM.welcome_confirm)
async def fsm_welcome_save(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    lang = data.get("welcome_lang", "uz")
    text = data.get("welcome_new_text", "")
    await state.clear()
    if not text:
        await callback.answer("❌ Matn topilmadi.", show_alert=True)
        return

    try:
        async with _session() as session:
            await WelcomeRepo(session).set(lang, text)
    except Exception as e:
        await callback.message.edit_text(f"❌ Saqlashda xatolik: {esc(e)}", parse_mode="HTML")
        await callback.answer()
        return

    await callback.message.edit_text(
        f"✅ <b>{_LANG_NAMES.get(lang, esc(lang))}</b> tili uchun xabar saqlandi.",
        parse_mode="HTML",
        reply_markup=admin_back_keyboard(),
    )
    await callback.answer()


# ── Export users (streamed to a temp file, never fully in memory) ────────────

EXPORT_COLUMNS = [
    "id", "telegram_id", "username", "first_name", "language",
    "joined_at", "last_active", "downloads_count", "recognitions_count",
    "searches_count", "is_banned", "blocked_bot",
]


def _fmt_dt(dt: Optional[datetime]) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if dt else ""


@router.callback_query(F.data == "adm:export")
async def cb_adm_export(callback: CallbackQuery):
    from bot.services.media import get_temp_path, safe_delete

    await callback.answer("⏳")
    await callback.message.edit_text("📤 CSV fayl tayyorlanmoqda...")

    path = get_temp_path(suffix=".csv", prefix="exp_")
    count = 0
    try:
        with open(path, "w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(EXPORT_COLUMNS)
            async with _session() as session:
                async for u in UserRepo(session).stream_for_export():
                    writer.writerow([
                        u.id, u.tg_id, u.username or "", u.first_name or "", u.lang,
                        _fmt_dt(u.joined_at), _fmt_dt(u.last_active),
                        u.downloads_count, u.recognitions_count, u.searches_count,
                        bool(u.is_blocked), bool(u.is_blocked_bot),
                    ])
                    count += 1

        filename = f"users_{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.csv"
        await callback.message.answer_document(
            document=FSInputFile(path, filename=filename),
            caption=f"👥 Jami {count} foydalanuvchi",
        )
        await callback.message.edit_text("✅ Export tayyor.", reply_markup=admin_back_keyboard())
    except Exception as e:
        logger.error(f"Export error: {e}")
        await callback.message.edit_text(f"❌ Export xatosi: {esc(e)}", parse_mode="HTML",
                                         reply_markup=admin_back_keyboard())
    finally:
        safe_delete(path)


# ── Ban / Unban ───────────────────────────────────────────────────────────────

@router.callback_query(F.data == "adm:ban")
async def cb_adm_ban(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminFSM.ban_get_id)
    await state.update_data(ban_action="ban")
    await callback.message.edit_text(
        "🚫 <b>Ban / Unban</b>\n\nAmalni tanlang, so'ng foydalanuvchi ID sini yuboring:",
        parse_mode="HTML",
        reply_markup=admin_ban_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data.in_({"adm:do_ban", "adm:do_unban"}), AdminFSM.ban_get_id)
async def fsm_ban_action_prompt(callback: CallbackQuery, state: FSMContext):
    action = "ban" if callback.data == "adm:do_ban" else "unban"
    await state.update_data(ban_action=action)
    prompt = ("🚫 Ban qilish uchun foydalanuvchi ID sini yuboring:" if action == "ban"
              else "✅ Unban qilish uchun foydalanuvchi ID sini yuboring:")
    await callback.message.edit_text(prompt, reply_markup=admin_back_keyboard("adm:ban"))
    await callback.answer()


def parse_user_id(text: Optional[str]) -> Optional[int]:
    raw = (text or "").strip()
    if not raw.isdigit() or len(raw) > 15:
        return None
    value = int(raw)
    return value if value > 0 else None


@router.message(AdminFSM.ban_get_id)
async def fsm_ban_id(message: Message, state: FSMContext):
    tg_id = parse_user_id(message.text)
    if tg_id is None:
        # Photo/sticker/garbage: re-ask, keep the FSM state
        await message.answer("❌ Noto'g'ri ID. Faqat raqam yuboring (masalan: 123456789).")
        return

    data = await state.get_data()
    action = data.get("ban_action", "ban")

    if action == "ban" and _is_admin(tg_id):
        await message.answer("⛔ Adminni ban qilib bo'lmaydi.", reply_markup=admin_back_keyboard())
        await state.clear()
        return

    await state.clear()
    try:
        from bot.services.cache import set_banned
        async with _session() as session:
            await UserRepo(session).set_blocked(tg_id, action == "ban")
        await set_banned(tg_id, action == "ban")
    except Exception as e:
        await message.answer(f"❌ Xato: {esc(e)}", parse_mode="HTML", reply_markup=admin_back_keyboard())
        return

    status = "🚫 Ban qilindi" if action == "ban" else "✅ Unban qilindi"
    await message.answer(f"{status}: <code>{tg_id}</code>", parse_mode="HTML", reply_markup=admin_back_keyboard())


# ── Required channel ──────────────────────────────────────────────────────────

@router.callback_query(F.data == "adm:channel")
async def cb_adm_channel(callback: CallbackQuery):
    try:
        async with _session() as session:
            repo = SettingRepo(session)
            ch_id = await repo.get("required_channel_id")
            ch_title = await repo.get("required_channel_title")
    except Exception:
        ch_id, ch_title = "", ""

    current = f"<b>{esc(ch_title)}</b> (<code>{esc(ch_id)}</code>)" if ch_id else "Yo'q"
    await callback.message.edit_text(
        f"📺 <b>Majburiy kanal</b>\n\nJoriy kanal: {current}",
        parse_mode="HTML",
        reply_markup=admin_channel_keyboard(has_channel=bool(ch_id)),
    )
    await callback.answer()


@router.callback_query(F.data == "adm:ch_set")
async def cb_adm_ch_set(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminFSM.channel_set)
    await callback.message.edit_text(
        "📺 Kanal username yoki ID sini yuboring (masalan: <code>@mychannel</code> yoki "
        "<code>-1001234567890</code>).\n\nBot kanalda <b>admin</b> bo'lishi shart.",
        parse_mode="HTML",
        reply_markup=admin_back_keyboard("adm:channel"),
    )
    await callback.answer()


def parse_channel_ref(text: Optional[str]) -> Optional[str | int]:
    raw = (text or "").strip()
    if raw.startswith("https://t.me/"):
        raw = "@" + raw.removeprefix("https://t.me/").strip("/")
    if raw.lstrip("-").isdigit():
        return int(raw)
    if raw.startswith("@") and 5 <= len(raw) <= 33 and raw[1:].replace("_", "").isalnum():
        return raw
    return None


@router.message(AdminFSM.channel_set)
async def fsm_channel_set(message: Message, state: FSMContext):
    ref = parse_channel_ref(message.text)
    if ref is None:
        await message.answer("❌ Noto'g'ri format. <code>@kanal</code> yoki <code>-100…</code> yuboring.",
                             parse_mode="HTML")
        return

    bot = message.bot
    try:
        chat = await bot.get_chat(ref)
    except Exception as e:
        await message.answer(f"❌ Kanal topilmadi: {esc(e)}", parse_mode="HTML",
                             reply_markup=admin_back_keyboard("adm:channel"))
        return

    # Without admin rights get_chat_member fails for every user and the
    # middleware fails open — the requirement would silently do nothing.
    try:
        me = await bot.me()
        member = await bot.get_chat_member(chat.id, me.id)
        is_admin = member.status in ("administrator", "creator")
    except Exception:
        is_admin = False
    if not is_admin:
        await message.answer(
            f"❌ Bot <b>{esc(chat.title)}</b> kanalida admin emas. Botni kanalga admin qilib qo'shing va qayta urining.",
            parse_mode="HTML", reply_markup=admin_back_keyboard("adm:channel"),
        )
        return

    if chat.username:
        invite = f"https://t.me/{chat.username}"
    else:
        try:
            invite = (await bot.create_chat_invite_link(chat.id, name="Yuklaydi bot")).invite_link
        except Exception as e:
            await message.answer(
                f"❌ Yopiq kanal uchun taklif havolasi yaratilmadi (botga \"Invite users\" huquqi kerak): {esc(e)}",
                parse_mode="HTML", reply_markup=admin_back_keyboard("adm:channel"),
            )
            return

    await state.clear()
    try:
        from bot.services.cache import invalidate_setting
        async with _session() as session:
            repo = SettingRepo(session)
            await repo.set("required_channel_id", str(chat.id))
            await repo.set("required_channel_title", chat.title or "")
            await repo.set("required_channel_url", invite)
        await invalidate_setting("required_channel_id")
    except Exception as e:
        await message.answer(f"❌ Saqlashda xato: {esc(e)}", parse_mode="HTML",
                             reply_markup=admin_back_keyboard("adm:channel"))
        return

    await message.answer(
        f"✅ Kanal o'rnatildi: <b>{esc(chat.title)}</b> (<code>{chat.id}</code>)",
        parse_mode="HTML",
        reply_markup=admin_back_keyboard("adm:channel"),
    )


@router.callback_query(F.data == "adm:ch_remove")
async def cb_adm_ch_remove(callback: CallbackQuery):
    try:
        from bot.services.cache import invalidate_setting
        async with _session() as session:
            repo = SettingRepo(session)
            await repo.set("required_channel_id", "")
            await repo.set("required_channel_title", "")
            await repo.set("required_channel_url", "")
        await invalidate_setting("required_channel_id")
    except Exception as e:
        await callback.message.answer(f"❌ Xato: {esc(e)}", parse_mode="HTML")
        await callback.answer()
        return
    await callback.message.edit_text("✅ Kanal o'chirildi.", reply_markup=admin_back_keyboard("adm:channel"))
    await callback.answer()


# ── Channel check callback (any user confirms subscription) ──────────────────
# Lives on its own router: the admin router is admin-only.

user_router = Router()


@user_router.callback_query(F.data == "channel:check")
async def cb_channel_check(callback: CallbackQuery, user_lang: str = "uz"):
    lang = user_lang
    try:
        async with _session() as session:
            ch_id_str = await SettingRepo(session).get("required_channel_id")

        if not ch_id_str:
            await callback.answer("✅")
            return

        member = await callback.bot.get_chat_member(int(ch_id_str), callback.from_user.id)
        if member.status in ("member", "administrator", "creator"):
            from bot.services.cache import set_cached_membership
            await set_cached_membership(callback.from_user.id, True)
            try:
                await callback.message.delete()
            except Exception:
                pass
            await callback.answer(t("channel.confirmed", lang), show_alert=True)
        else:
            await callback.answer(t("channel.not_subscribed", lang), show_alert=True)
    except Exception as e:
        logger.warning(f"Channel check failed: {e}")
        await callback.answer(t("channel.check_failed", lang), show_alert=True)

