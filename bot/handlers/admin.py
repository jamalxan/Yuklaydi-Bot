from __future__ import annotations

import asyncio
import csv
import io
from datetime import datetime

from aiogram import Bot, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    BufferedInputFile, CallbackQuery, InlineKeyboardButton,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.config import settings
from bot.db.repo import (
    DownloadRepo, SettingRepo,
    UserRepo, WelcomeRepo,
)
from bot.handlers.states import AdminFSM
from bot.keyboards.inline import (
    admin_back_keyboard, admin_ban_keyboard, admin_broadcast_confirm_keyboard,
    admin_broadcast_skip_keyboard, admin_channel_keyboard,
    admin_main_keyboard, admin_welcome_confirm_keyboard,
    admin_welcome_lang_keyboard,
)
from bot.logger import logger

router = Router()

_BROADCAST_RATE = 25  # messages per second


def _is_admin(user_id: int) -> bool:
    return user_id in settings.admin_ids


def _admin_filter(m) -> bool:
    u = getattr(m, "from_user", None)
    return bool(u and _is_admin(u.id))


# ── Commands ──────────────────────────────────────────────────────────────────

@router.message(Command("admin"))
async def cmd_admin(message: Message, user_repo: UserRepo, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    await state.clear()
    await message.answer("🛠 <b>Admin Panel</b>", parse_mode="HTML", reply_markup=admin_main_keyboard())


@router.message(Command("statistics"), lambda m: _admin_filter(m))
async def cmd_statistics(message: Message, user_repo: UserRepo, download_repo: DownloadRepo):
    if not _is_admin(message.from_user.id):
        return
    text = await _build_stats(user_repo, download_repo)
    await message.answer(text, parse_mode="HTML")


# ── Admin panel main callback ─────────────────────────────────────────────────

@router.callback_query(lambda c: c.data == "adm:main")
async def cb_adm_main(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    await state.clear()
    try:
        await callback.message.edit_text("🛠 <b>Admin Panel</b>", parse_mode="HTML", reply_markup=admin_main_keyboard())
    except TelegramBadRequest:
        await callback.message.answer("🛠 <b>Admin Panel</b>", parse_mode="HTML", reply_markup=admin_main_keyboard())
    await callback.answer()


@router.callback_query(lambda c: c.data == "adm:close")
async def cb_adm_close(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    await state.clear()
    await callback.message.delete()
    await callback.answer()


# ── Statistics ────────────────────────────────────────────────────────────────

async def _build_stats(user_repo: UserRepo, download_repo: DownloadRepo) -> str:
    try:
        from bot.db.session import async_session_factory
        from bot.db.repo import MusicSearchRepo, RecognitionRepo
        async with async_session_factory() as session:
            u_total = await UserRepo(session).count_all()
            u_today = await UserRepo(session).count_new_today()
            u_week = await UserRepo(session).count_new_week()
            u_active7 = await UserRepo(session).count_active(7)
            u_active30 = await UserRepo(session).count_active(30)
        async with async_session_factory() as session:
            dl_total = await DownloadRepo(session).count_all()
            dl_today = await DownloadRepo(session).count_today()
            top = await DownloadRepo(session).top_platforms(5)
            dl_cache = await DownloadRepo(session).count_today_cache_hits()
        async with async_session_factory() as session:
            rec_total = await RecognitionRepo(session).count_all()
            rec_today = await RecognitionRepo(session).count_today()
        async with async_session_factory() as session:
            srch_total = await MusicSearchRepo(session).count_all()
            srch_today = await MusicSearchRepo(session).count_today()
    except Exception as e:
        logger.error(f"Stats error: {e}")
        return "❌ Stats unavailable."

    top_lines = "\n".join(
        f"  {i + 1}. {row[0] or 'unknown'}: {row[1]}" for i, row in enumerate(top)
    ) or "  —"
    cache_rate = f"{dl_cache}/{dl_today}" if dl_today else "0/0"

    # Pull timing metrics from Redis
    avg_t = "—"
    try:
        from bot.services.cache import get_redis
        r = await get_redis()
        t_sum_str = await r.get("stat:t_total_sum")
        t_cnt_str = await r.get("stat:t_total_count")
        if t_sum_str and t_cnt_str:
            t_sum = float(t_sum_str)
            t_cnt = int(t_cnt_str)
            if t_cnt > 0:
                avg_t = f"{t_sum / t_cnt:.1f}s (n={t_cnt})"
    except Exception:
        pass

    return (
        "📊 <b>Statistika</b>\n\n"
        f"👥 <b>Foydalanuvchilar:</b>\n"
        f"  Jami: <b>{u_total}</b> | Bugun: <b>{u_today}</b> | Hafta: <b>{u_week}</b>\n"
        f"  Faol 7 kun: <b>{u_active7}</b> | Faol 30 kun: <b>{u_active30}</b>\n\n"
        f"🎬 <b>Yuklamalar:</b> jami <b>{dl_total}</b> | bugun <b>{dl_today}</b>\n"
        f"🎵 <b>Aniqlashlar:</b> jami <b>{rec_total}</b> | bugun <b>{rec_today}</b>\n"
        f"🔍 <b>Qidiruvlar:</b> jami <b>{srch_total}</b> | bugun <b>{srch_today}</b>\n\n"
        f"🏆 <b>Top platformalar:</b>\n{top_lines}\n\n"
        f"⚡ <b>Kesh (bugun):</b> {cache_rate}\n"
        f"⏱ <b>O'rtacha vaqt:</b> {avg_t}"
    )


@router.callback_query(lambda c: c.data == "adm:stats")
async def cb_adm_stats(callback: CallbackQuery, user_repo: UserRepo, download_repo: DownloadRepo):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    await callback.answer("⏳")
    text = await _build_stats(user_repo, download_repo)
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="⬅️ Orqaga", callback_data="adm:main"))
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=builder.as_markup())
    except TelegramBadRequest:
        await callback.message.answer(text, parse_mode="HTML", reply_markup=builder.as_markup())


# ── Broadcast ─────────────────────────────────────────────────────────────────

@router.callback_query(lambda c: c.data == "adm:broadcast")
async def cb_adm_broadcast(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    await state.set_state(AdminFSM.broadcast_content)
    text = "📢 <b>Broadcast</b>\n\nXabar yuboring (matn, rasm yoki video). Caption ham qo'shishingiz mumkin."
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="❌ Bekor qilish", callback_data="adm:main"))
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=builder.as_markup())
    await callback.answer()


@router.message(AdminFSM.broadcast_content)
async def fsm_broadcast_content(message: Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return

    content: dict = {
        "chat_id": message.chat.id,
        "message_id": message.message_id,
        "content_type": message.content_type,
        "caption": None,
        "cta_text": None,
        "cta_url": None,
    }
    if message.caption:
        content["caption"] = message.caption
    elif message.text:
        content["caption"] = message.text

    await state.update_data(broadcast=content)
    await state.set_state(AdminFSM.broadcast_button)
    await message.answer(
        "🔘 CTA tugmasi qo'shish? <code>Matn - https://url</code> formatida yuboring yoki o'tkazib yuboring.",
        parse_mode="HTML",
        reply_markup=admin_broadcast_skip_keyboard(),
    )


@router.callback_query(lambda c: c.data == "adm:bc_skip_btn", AdminFSM.broadcast_button)
async def fsm_bc_skip_btn(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    await callback.answer()
    await _show_broadcast_preview(callback.message, callback.from_user.id, state, callback.bot)


@router.message(AdminFSM.broadcast_button)
async def fsm_broadcast_button(message: Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return

    text = message.text or ""
    cta_text, cta_url = None, None
    if " - " in text:
        parts = text.split(" - ", 1)
        cta_text = parts[0].strip()
        cta_url = parts[1].strip()
        if not cta_url.startswith("http"):
            await message.answer("❌ Noto'g'ri format. <code>Matn - https://url</code> yuboring.", parse_mode="HTML")
            return

    data = await state.get_data()
    bc = data.get("broadcast", {})
    bc["cta_text"] = cta_text
    bc["cta_url"] = cta_url
    await state.update_data(broadcast=bc)
    await _show_broadcast_preview(message, message.from_user.id, state, message.bot)


async def _show_broadcast_preview(msg_or_obj, user_id: int, state: FSMContext, bot: Bot):
    data = await state.get_data()
    bc = data.get("broadcast", {})

    # Get recipient count
    recipient_count: int | str
    try:
        from bot.db.session import async_session_factory
        async with async_session_factory() as session:
            recipient_ids = await UserRepo(session).get_all_ids()
        recipient_count = len(recipient_ids)
    except Exception:
        recipient_count = "?"

    # Build CTA keyboard if provided
    reply_markup = None
    if bc.get("cta_text") and bc.get("cta_url"):
        b = InlineKeyboardBuilder()
        b.row(InlineKeyboardButton(text=bc["cta_text"], url=bc["cta_url"]))
        reply_markup = b.as_markup()

    # Show preview
    preview_header = f"👁 <b>Ko'rinishi (preview):</b>\n📤 Qabul qiluvchilar: <b>{recipient_count}</b>\n\n"
    await msg_or_obj.answer(preview_header, parse_mode="HTML")

    try:
        await bot.copy_message(
            chat_id=msg_or_obj.chat.id,
            from_chat_id=bc["chat_id"],
            message_id=bc["message_id"],
            reply_markup=reply_markup,
        )
    except Exception as e:
        await msg_or_obj.answer(f"Preview error: {e}")

    await state.set_state(AdminFSM.broadcast_confirm)
    await msg_or_obj.answer(
        "✅ Yuborish uchun tasdiqlang:",
        reply_markup=admin_broadcast_confirm_keyboard(),
    )


@router.callback_query(lambda c: c.data == "adm:bc_cancel")
async def fsm_bc_cancel(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    await state.clear()
    await callback.message.edit_text("❌ Broadcast bekor qilindi.")
    await callback.answer()
    await callback.message.answer("🛠 <b>Admin Panel</b>", parse_mode="HTML", reply_markup=admin_main_keyboard())


@router.callback_query(lambda c: c.data == "adm:bc_send", AdminFSM.broadcast_confirm)
async def fsm_bc_send(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return

    data = await state.get_data()
    bc = data.get("broadcast", {})
    await state.clear()

    await callback.message.edit_text("📤 Yuborilmoqda...")
    await callback.answer()

    # Build CTA keyboard
    reply_markup = None
    if bc.get("cta_text") and bc.get("cta_url"):
        b = InlineKeyboardBuilder()
        b.row(InlineKeyboardButton(text=bc["cta_text"], url=bc["cta_url"]))
        reply_markup = b.as_markup()

    bot = callback.bot
    start_time = asyncio.get_event_loop().time()

    try:
        from bot.db.session import async_session_factory
        async with async_session_factory() as session:
            recipient_ids = await UserRepo(session).get_all_ids()
    except Exception:
        await callback.message.answer("❌ Could not get user list.")
        return

    delivered = 0
    failed = 0

    for i, tg_id in enumerate(recipient_ids):
        try:
            await bot.copy_message(
                chat_id=tg_id,
                from_chat_id=bc["chat_id"],
                message_id=bc["message_id"],
                reply_markup=reply_markup,
            )
            delivered += 1
        except (TelegramForbiddenError, TelegramBadRequest):
            failed += 1
        except Exception as e:
            logger.warning(f"Broadcast skip {tg_id}: {e}")
            failed += 1

        if (i + 1) % _BROADCAST_RATE == 0:
            await asyncio.sleep(1)

    duration = int(asyncio.get_event_loop().time() - start_time)
    report = (
        f"📊 <b>Broadcast yakunlandi</b>\n\n"
        f"✅ Yetkazildi: <b>{delivered}</b>\n"
        f"⚠️ Muvaffaqiyatsiz: <b>{failed}</b>\n"
        f"⏱ Vaqt: <b>{duration}s</b>"
    )
    await callback.message.answer(report, parse_mode="HTML", reply_markup=admin_back_keyboard())


# ── Edit welcome message ──────────────────────────────────────────────────────

@router.callback_query(lambda c: c.data == "adm:welcome")
async def cb_adm_welcome(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    await state.set_state(AdminFSM.welcome_lang)
    text = "✏️ <b>Xush kelibsiz xabarini tahrirlash</b>\n\nQaysi til uchun?"
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=admin_welcome_lang_keyboard())
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("adm:wl_") and len(c.data) <= 10)
async def fsm_welcome_lang(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return

    lang = callback.data.split("_")[-1]
    if lang not in ("uz", "ru", "en"):
        await callback.answer()
        return

    await state.update_data(welcome_lang=lang)
    await state.set_state(AdminFSM.welcome_new_text)

    # Show current text
    try:
        from bot.db.session import async_session_factory
        async with async_session_factory() as session:
            current = await WelcomeRepo(session).get(lang) or "—"
    except Exception:
        current = "—"

    lang_names = {"uz": "O'zbek", "ru": "Русский", "en": "English"}
    text = (
        f"✏️ <b>{lang_names[lang]}</b> uchun joriy xabar:\n\n"
        f"<code>{current}</code>\n\n"
        f"Yangi xabarni yuboring. Qo'llash mumkin: {{first_name}}, {{username}}"
    )
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="❌ Bekor", callback_data="adm:welcome"))
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=builder.as_markup())
    await callback.answer()


@router.message(AdminFSM.welcome_new_text)
async def fsm_welcome_new_text(message: Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return

    new_text = message.text or ""
    if not new_text.strip():
        await message.answer("❌ Matn bo'sh bo'lmasligi kerak.")
        return

    await state.update_data(welcome_new_text=new_text)
    await state.set_state(AdminFSM.welcome_confirm)

    preview = new_text.replace("{first_name}", "Ali").replace("{username}", "@ali")

    await message.answer(
        f"👁 <b>Preview:</b>\n\n{preview}\n\n<b>Saqlaysizmi?</b>",
        parse_mode="HTML",
        reply_markup=admin_welcome_confirm_keyboard(),
    )


@router.callback_query(lambda c: c.data == "adm:wl_save", AdminFSM.welcome_confirm)
async def fsm_welcome_save(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return

    data = await state.get_data()
    lang = data.get("welcome_lang", "uz")
    text = data.get("welcome_new_text", "")
    await state.clear()

    try:
        from bot.db.session import async_session_factory
        async with async_session_factory() as session:
            await WelcomeRepo(session).set(lang, text)
    except Exception as e:
        await callback.message.edit_text(f"❌ Saqlashda xatolik: {e}")
        await callback.answer()
        return

    lang_names = {"uz": "O'zbek", "ru": "Русский", "en": "English"}
    await callback.message.edit_text(
        f"✅ <b>{lang_names.get(lang, lang)}</b> tili uchun xabar saqlandi.",
        parse_mode="HTML",
        reply_markup=admin_back_keyboard(),
    )
    await callback.answer()


# ── Export users ──────────────────────────────────────────────────────────────

@router.callback_query(lambda c: c.data == "adm:export")
async def cb_adm_export(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return

    await callback.answer("⏳")
    await callback.message.edit_text("📤 CSV fayl tayyorlanmoqda...")

    try:
        from bot.db.session import async_session_factory
        async with async_session_factory() as session:
            users = await UserRepo(session).get_all_for_export()

        buf = io.BytesIO()
        buf.write(b"\xef\xbb\xbf")  # UTF-8 BOM
        wrapper = io.TextIOWrapper(buf, encoding="utf-8", newline="")
        writer = csv.writer(wrapper)
        writer.writerow([
            "id", "telegram_id", "username", "first_name", "language",
            "joined_at", "last_active", "downloads_count", "recognitions_count",
            "searches_count", "is_banned",
        ])
        for u in users:
            writer.writerow([
                u.id, u.tg_id, u.username or "", u.first_name or "",
                u.lang,
                u.joined_at.strftime("%Y-%m-%d %H:%M:%S") if u.joined_at else "",
                u.last_active.strftime("%Y-%m-%d %H:%M:%S") if u.last_active else "",
                u.downloads_count, u.recognitions_count, u.searches_count,
                u.is_blocked,
            ])
        wrapper.flush()
        csv_bytes = buf.getvalue()

        filename = f"users_{datetime.now().strftime('%Y-%m-%d')}.csv"
        await callback.message.answer_document(
            document=BufferedInputFile(csv_bytes, filename=filename),
            caption=f"👥 Jami {len(users)} foydalanuvchi",
        )
        await callback.message.edit_text("✅ Export tayyor.", reply_markup=admin_back_keyboard())
    except Exception as e:
        logger.error(f"Export error: {e}")
        await callback.message.edit_text(f"❌ Export xatosi: {e}", reply_markup=admin_back_keyboard())


# ── Ban / Unban ───────────────────────────────────────────────────────────────

@router.callback_query(lambda c: c.data == "adm:ban")
async def cb_adm_ban(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    await state.set_state(AdminFSM.ban_get_id)
    await callback.message.edit_text(
        "🚫 <b>Ban / Unban</b>\n\nFoydalanuvchi ID sini yuboring:",
        parse_mode="HTML",
        reply_markup=admin_ban_keyboard(),
    )
    await callback.answer()


@router.callback_query(lambda c: c.data == "adm:do_ban", AdminFSM.ban_get_id)
async def fsm_do_ban_prompt(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    await state.update_data(ban_action="ban")
    await callback.message.edit_text(
        "🚫 Ban qilish uchun foydalanuvchi ID sini yuboring:", reply_markup=admin_back_keyboard("adm:ban")
    )
    await callback.answer()


@router.callback_query(lambda c: c.data == "adm:do_unban", AdminFSM.ban_get_id)
async def fsm_do_unban_prompt(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    await state.update_data(ban_action="unban")
    await callback.message.edit_text(
        "✅ Unban qilish uchun foydalanuvchi ID sini yuboring:", reply_markup=admin_back_keyboard("adm:ban")
    )
    await callback.answer()


@router.message(AdminFSM.ban_get_id)
async def fsm_ban_id(message: Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return

    try:
        tg_id = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Noto'g'ri ID. Raqam yuboring.")
        return

    data = await state.get_data()
    action = data.get("ban_action", "ban")
    await state.clear()

    try:
        from bot.db.session import async_session_factory
        async with async_session_factory() as session:
            user = await UserRepo(session).get(tg_id)
            if not user:
                await message.answer("❌ Foydalanuvchi topilmadi.")
                await message.answer("🛠 <b>Admin Panel</b>", parse_mode="HTML", reply_markup=admin_main_keyboard())
                return
            await UserRepo(session).set_blocked(tg_id, action == "ban")
    except Exception as e:
        await message.answer(f"❌ Xato: {e}")
        return

    status = "🚫 Ban qilindi" if action == "ban" else "✅ Unban qilindi"
    await message.answer(f"{status}: <code>{tg_id}</code>", parse_mode="HTML", reply_markup=admin_back_keyboard())


# ── Required channel ──────────────────────────────────────────────────────────

@router.callback_query(lambda c: c.data == "adm:channel")
async def cb_adm_channel(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    try:
        from bot.db.session import async_session_factory
        async with async_session_factory() as session:
            ch_id = await SettingRepo(session).get("required_channel_id")
    except Exception:
        ch_id = ""

    ch_display = ch_id if ch_id else "Yo'q"
    text = (
        f"📺 <b>Majburiy kanal</b>\n\n"
        f"Joriy kanal: <code>{ch_display}</code>"
    )
    await callback.message.edit_text(
        text, parse_mode="HTML",
        reply_markup=admin_channel_keyboard(has_channel=bool(ch_id)),
    )
    await callback.answer()


@router.callback_query(lambda c: c.data == "adm:ch_set")
async def cb_adm_ch_set(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    await state.set_state(AdminFSM.channel_set)
    await callback.message.edit_text(
        "📺 Kanal username yoki ID sini yuboring (masalan: <code>@mychannel</code> yoki <code>-1001234567890</code>)",
        parse_mode="HTML",
        reply_markup=admin_back_keyboard("adm:channel"),
    )
    await callback.answer()


@router.message(AdminFSM.channel_set)
async def fsm_channel_set(message: Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return

    ch = message.text.strip()
    await state.clear()

    try:
        chat = await message.bot.get_chat(ch)
        invite = f"https://t.me/{chat.username}" if chat.username else ""

        from bot.db.session import async_session_factory
        from bot.services.cache import invalidate_setting
        async with async_session_factory() as session:
            repo = SettingRepo(session)
            await repo.set("required_channel_id", str(chat.id))
            await repo.set("required_channel_title", chat.title or "")
            await repo.set("required_channel_url", invite)
        await invalidate_setting("required_channel_id")
        await message.answer(
            f"✅ Kanal o'rnatildi: <b>{chat.title}</b> (<code>{chat.id}</code>)",
            parse_mode="HTML",
            reply_markup=admin_back_keyboard("adm:channel"),
        )
    except Exception as e:
        await message.answer(f"❌ Xato: {e}\nBot kanalga admin bo'lishi kerak.", reply_markup=admin_back_keyboard("adm:channel"))


@router.callback_query(lambda c: c.data == "adm:ch_remove")
async def cb_adm_ch_remove(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    try:
        from bot.db.session import async_session_factory
        from bot.services.cache import invalidate_setting
        async with async_session_factory() as session:
            repo = SettingRepo(session)
            await repo.set("required_channel_id", "")
            await repo.set("required_channel_title", "")
            await repo.set("required_channel_url", "")
        await invalidate_setting("required_channel_id")
    except Exception as e:
        await callback.message.answer(f"❌ Xato: {e}")
        await callback.answer()
        return
    await callback.message.edit_text(
        "✅ Kanal o'chirildi.",
        reply_markup=admin_back_keyboard("adm:channel"),
    )
    await callback.answer()


# ── Channel check callback (user confirms subscription) ───────────────────────

@router.callback_query(lambda c: c.data == "channel:check")
async def cb_channel_check(callback: CallbackQuery):
    try:
        from bot.db.session import async_session_factory
        async with async_session_factory() as session:
            ch_id_str = await SettingRepo(session).get("required_channel_id")

        if not ch_id_str:
            await callback.answer("✅", show_alert=False)
            return

        member = await callback.bot.get_chat_member(int(ch_id_str), callback.from_user.id)
        if member.status in ("member", "administrator", "creator"):
            from bot.services.cache import set_cached_membership
            await set_cached_membership(callback.from_user.id, True)
            await callback.message.delete()
            await callback.answer(
                {"uz": "✅ Obuna tasdiqlandi!", "ru": "✅ Подписка подтверждена!", "en": "✅ Subscription confirmed!"}.get("en", "✅"),
                show_alert=True,
            )
        else:
            await callback.answer(
                {"uz": "❌ Siz hali obuna bo'lmagansiz!", "ru": "❌ Вы ещё не подписались!", "en": "❌ You haven't subscribed yet!"}.get("en", "❌"),
                show_alert=True,
            )
    except Exception:
        await callback.answer("❌ Tekshirib bo'lmadi.", show_alert=True)
