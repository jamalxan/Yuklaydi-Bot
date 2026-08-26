"""
download_task's single-flight wait loop must notice when the lock frees up
early (the original holder failed fast) and grab it itself, instead of always
idling for the full job_timeout before downloading.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_waiter_reacquires_lock_after_early_release():
    from bot.worker import tasks as tasks_mod

    acquire_calls = {"n": 0}

    async def fake_acquire(url, quality, ttl):
        acquire_calls["n"] += 1
        # First attempt: someone else holds the lock. Second attempt (after the
        # first 1s wait tick): that holder already failed and released it.
        return acquire_calls["n"] >= 2

    fake_result = MagicMock(path="/tmp/fake.mp4", size_bytes=1000, t_extract=0.1)

    fake_sent = MagicMock()
    fake_sent.video = MagicMock(file_id="new_file_id")
    fake_sent.message_id = 555

    fake_bot = MagicMock()
    fake_bot.session = MagicMock(close=AsyncMock())
    fake_bot.edit_message_text = AsyncMock()
    fake_bot.send_chat_action = AsyncMock()
    fake_bot.send_video = AsyncMock(return_value=fake_sent)
    fake_bot.delete_message = AsyncMock()
    fake_bot.edit_message_reply_markup = AsyncMock()

    fake_download = AsyncMock(return_value=fake_result)

    with patch.object(tasks_mod, "acquire_single_flight", side_effect=fake_acquire), \
         patch.object(tasks_mod, "get_file_id", new=AsyncMock(return_value=None)), \
         patch.object(tasks_mod, "acquire_user_slot", new=AsyncMock()), \
         patch.object(tasks_mod, "release_user_slot", new=AsyncMock()), \
         patch.object(tasks_mod, "release_single_flight", new=AsyncMock()) as mock_release, \
         patch.object(tasks_mod, "store_video_for_shazam", new=AsyncMock(return_value="shzkey")), \
         patch.object(tasks_mod, "store_url_key", new=AsyncMock(return_value="urlkey")), \
         patch.object(tasks_mod, "set_file_id", new=AsyncMock()), \
         patch.object(tasks_mod, "download", fake_download), \
         patch.object(tasks_mod, "get_temp_path", return_value="/tmp/fake.mp4"), \
         patch.object(tasks_mod, "safe_delete"), \
         patch.object(tasks_mod, "_make_bot", return_value=fake_bot), \
         patch("os.path.exists", return_value=True), \
         patch("asyncio.sleep", new=AsyncMock()):

        await tasks_mod.download_task(
            {}, user_id=1, chat_id=1, message_id=1,
            url="https://youtube.com/watch?v=X", platform="youtube",
            quality="720", user_lang="en",
        )

    # Only 2 acquire attempts needed (not looping for the full job_timeout) —
    # the second one succeeded, so we must have downloaded ourselves.
    assert acquire_calls["n"] == 2
    fake_download.assert_called_once()
    fake_bot.send_video.assert_called_once()
    # We won the lock ourselves on the 2nd attempt — must release it when done.
    mock_release.assert_called_once()


@pytest.mark.asyncio
async def test_waiter_serves_from_cache_when_file_id_appears():
    """If the original holder finishes successfully, the waiter must serve the
    cached file_id instead of downloading a second time."""
    from bot.worker import tasks as tasks_mod

    get_file_id_calls = {"n": 0}

    async def fake_get_file_id(url, quality):
        get_file_id_calls["n"] += 1
        return "cached_file_id" if get_file_id_calls["n"] >= 2 else None

    fake_bot = MagicMock()
    fake_bot.session = MagicMock(close=AsyncMock())
    fake_bot.edit_message_text = AsyncMock()
    fake_bot.send_chat_action = AsyncMock()
    fake_bot.send_video = AsyncMock()
    fake_bot.delete_message = AsyncMock()

    fake_download = AsyncMock()

    with patch.object(tasks_mod, "acquire_single_flight", new=AsyncMock(return_value=False)), \
         patch.object(tasks_mod, "get_file_id", side_effect=fake_get_file_id), \
         patch.object(tasks_mod, "acquire_user_slot", new=AsyncMock()), \
         patch.object(tasks_mod, "release_user_slot", new=AsyncMock()), \
         patch.object(tasks_mod, "release_single_flight", new=AsyncMock()) as mock_release, \
         patch.object(tasks_mod, "store_video_for_shazam", new=AsyncMock(return_value="shzkey")), \
         patch.object(tasks_mod, "store_url_key", new=AsyncMock(return_value="urlkey")), \
         patch.object(tasks_mod, "download", fake_download), \
         patch.object(tasks_mod, "get_temp_path", return_value="/tmp/fake.mp4"), \
         patch.object(tasks_mod, "safe_delete"), \
         patch.object(tasks_mod, "_make_bot", return_value=fake_bot), \
         patch("asyncio.sleep", new=AsyncMock()):

        await tasks_mod.download_task(
            {}, user_id=1, chat_id=1, message_id=1,
            url="https://youtube.com/watch?v=X", platform="youtube",
            quality="720", user_lang="en",
        )

    fake_download.assert_not_called()
    fake_bot.send_video.assert_called_once()
    _, call_kwargs = fake_bot.send_video.call_args
    assert call_kwargs["video"] == "cached_file_id"
    # Never won the lock ourselves — nothing to release.
    mock_release.assert_not_called()
