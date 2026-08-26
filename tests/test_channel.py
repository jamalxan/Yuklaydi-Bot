"""
Unit tests for ChannelCheckMiddleware.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_message(text: str = "hello", user_id: int = 12345) -> MagicMock:
    msg = MagicMock()
    msg.text = text
    msg.from_user = MagicMock()
    msg.from_user.id = user_id
    msg.bot = AsyncMock()
    msg.answer = AsyncMock()
    return msg


async def _noop_handler(event, data):
    return "handled"


# ── Skip commands always pass through ────────────────────────────────────────

@pytest.mark.asyncio
async def test_skip_start_command():
    from bot.middlewares.channel import ChannelCheckMiddleware
    mw = ChannelCheckMiddleware()
    msg = _make_message("/start")

    with patch("bot.middlewares.channel._get_channel_id", new=AsyncMock(return_value="-100123456789")):
        result = await mw(_noop_handler, msg, {"user_lang": "uz"})

    assert result == "handled"
    msg.answer.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("cmd", ["/admin", "/lang", "/help", "/statistics"])
async def test_skip_all_exempt_commands(cmd):
    from bot.middlewares.channel import ChannelCheckMiddleware
    mw = ChannelCheckMiddleware()
    msg = _make_message(cmd)

    with patch("bot.middlewares.channel._get_channel_id", new=AsyncMock(return_value="-100999")):
        result = await mw(_noop_handler, msg, {"user_lang": "ru"})

    assert result == "handled"
    msg.answer.assert_not_called()


# ── No channel configured → pass through ─────────────────────────────────────

@pytest.mark.asyncio
async def test_no_channel_configured_passes_through():
    from bot.middlewares.channel import ChannelCheckMiddleware
    mw = ChannelCheckMiddleware()
    msg = _make_message("some text")

    with patch("bot.middlewares.channel._get_channel_id", new=AsyncMock(return_value="")):
        result = await mw(_noop_handler, msg, {"user_lang": "en"})

    assert result == "handled"
    msg.answer.assert_not_called()


# ── Subscribed user → pass through ───────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["member", "administrator", "creator"])
async def test_subscribed_user_passes_through(status):
    from bot.middlewares.channel import ChannelCheckMiddleware
    mw = ChannelCheckMiddleware()
    msg = _make_message("download this video")

    member_mock = MagicMock()
    member_mock.status = status
    msg.bot.get_chat_member = AsyncMock(return_value=member_mock)

    with patch("bot.middlewares.channel._get_channel_id", new=AsyncMock(return_value="-100123")):
        result = await mw(_noop_handler, msg, {"user_lang": "uz"})

    assert result == "handled"
    msg.answer.assert_not_called()


# ── Non-subscribed user → join prompt, handler blocked ───────────────────────

@pytest.mark.asyncio
async def test_non_subscriber_gets_join_prompt():
    from bot.middlewares.channel import ChannelCheckMiddleware
    mw = ChannelCheckMiddleware()
    msg = _make_message("https://youtube.com/watch?v=abc")

    member_mock = MagicMock()
    member_mock.status = "left"
    msg.bot.get_chat_member = AsyncMock(return_value=member_mock)

    with (
        patch("bot.middlewares.channel._get_channel_id", new=AsyncMock(return_value="-100123")),
        patch("bot.middlewares.channel.async_session_factory") as mock_factory,
    ):
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_factory.return_value = mock_session

        repo_mock = AsyncMock()
        repo_mock.get = AsyncMock(side_effect=lambda k, d="": {"required_channel_url": "https://t.me/test", "required_channel_title": "Test"}.get(k, d))

        with patch("bot.middlewares.channel.SettingRepo", return_value=repo_mock):
            result = await mw(_noop_handler, msg, {"user_lang": "uz"})

    # Handler must NOT have been called
    assert result is None
    # Join prompt must have been sent
    msg.answer.assert_called_once()
    answer_call = msg.answer.call_args
    text_arg = answer_call[0][0] if answer_call[0] else answer_call[1].get("text", "")
    assert "obuna" in text_arg.lower() or "subscribe" in text_arg.lower() or "Подпишитесь" in text_arg


@pytest.mark.asyncio
async def test_non_subscriber_prompt_includes_keyboard():
    from bot.middlewares.channel import ChannelCheckMiddleware
    mw = ChannelCheckMiddleware()
    msg = _make_message("https://youtube.com/watch?v=xyz")

    member_mock = MagicMock()
    member_mock.status = "kicked"
    msg.bot.get_chat_member = AsyncMock(return_value=member_mock)

    with (
        patch("bot.middlewares.channel._get_channel_id", new=AsyncMock(return_value="-100456")),
        patch("bot.middlewares.channel.async_session_factory") as mock_factory,
    ):
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_factory.return_value = mock_session

        repo_mock = AsyncMock()
        repo_mock.get = AsyncMock(return_value="")

        with patch("bot.middlewares.channel.SettingRepo", return_value=repo_mock):
            await mw(_noop_handler, msg, {"user_lang": "en"})

    # Must be called with reply_markup
    call_kwargs = msg.answer.call_args[1]
    assert "reply_markup" in call_kwargs
    assert call_kwargs["reply_markup"] is not None


# ── Admins bypass the channel wall entirely ──────────────────────────────────

@pytest.mark.asyncio
async def test_admin_bypasses_channel_check_even_when_not_subscribed():
    """Admins must never get walled off by the required-channel check — including
    on the plain-text FSM steps of their own admin panel (broadcast text, ban ID,
    welcome message, channel id) which aren't in _SKIP_COMMANDS."""
    from bot.middlewares.channel import ChannelCheckMiddleware

    with patch("bot.middlewares.channel.settings") as mock_settings:
        mock_settings.admin_ids = [12345]
        mw = ChannelCheckMiddleware()
        msg = _make_message("-1001234567890", user_id=12345)  # e.g. typing a channel id

        # get_chat_member would report "left" if it were ever called
        member_mock = MagicMock()
        member_mock.status = "left"
        msg.bot.get_chat_member = AsyncMock(return_value=member_mock)

        with patch("bot.middlewares.channel._get_channel_id", new=AsyncMock(return_value="-100123")):
            result = await mw(_noop_handler, msg, {"user_lang": "uz"})

    assert result == "handled"
    msg.answer.assert_not_called()
    msg.bot.get_chat_member.assert_not_called()


@pytest.mark.asyncio
async def test_non_admin_still_walled_off():
    """Sanity check: the admin bypass must not accidentally exempt everyone."""
    from bot.middlewares.channel import ChannelCheckMiddleware

    with patch("bot.middlewares.channel.settings") as mock_settings:
        mock_settings.admin_ids = [12345]
        mw = ChannelCheckMiddleware()
        msg = _make_message("hello", user_id=99999)

        member_mock = MagicMock()
        member_mock.status = "left"
        msg.bot.get_chat_member = AsyncMock(return_value=member_mock)

        with (
            patch("bot.middlewares.channel._get_channel_id", new=AsyncMock(return_value="-100123")),
            patch("bot.middlewares.channel.async_session_factory") as mock_factory,
        ):
            mock_session = AsyncMock()
            mock_session.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session.__aexit__ = AsyncMock(return_value=False)
            mock_factory.return_value = mock_session
            repo_mock = AsyncMock()
            repo_mock.get = AsyncMock(return_value="")
            with patch("bot.middlewares.channel.SettingRepo", return_value=repo_mock):
                result = await mw(_noop_handler, msg, {"user_lang": "uz"})

    assert result is None
    msg.answer.assert_called_once()


# ── API error → pass through (fail-open for resilience) ───────────────────────

@pytest.mark.asyncio
async def test_api_error_passes_through():
    from bot.middlewares.channel import ChannelCheckMiddleware
    mw = ChannelCheckMiddleware()
    msg = _make_message("https://tiktok.com/@user/video/123")

    msg.bot.get_chat_member = AsyncMock(side_effect=Exception("Forbidden"))

    with patch("bot.middlewares.channel._get_channel_id", new=AsyncMock(return_value="-100999")):
        result = await mw(_noop_handler, msg, {"user_lang": "ru"})

    assert result == "handled"


# ── join/check button labels are non-empty ────────────────────────────────────

def test_join_messages_non_empty():
    from bot.middlewares.channel import _JOIN_MSGS, _JOIN_BTN, _CHECK_BTN
    for lang in ("uz", "ru", "en"):
        assert _JOIN_MSGS[lang], f"_JOIN_MSGS[{lang}] is empty"
        assert _JOIN_BTN[lang], f"_JOIN_BTN[{lang}] is empty"
        assert _CHECK_BTN[lang], f"_CHECK_BTN[{lang}] is empty"
