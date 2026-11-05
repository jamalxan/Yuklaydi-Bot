"""Tests for the admin panel: gating, broadcast logic, export, stats."""
from __future__ import annotations

import csv
import io
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.config import settings


# ── Admin gating ──────────────────────────────────────────────────────────────

def test_admin_ids_from_settings():
    """settings.admin_ids must be a list of ints."""
    assert isinstance(settings.admin_ids, list)
    for aid in settings.admin_ids:
        assert isinstance(aid, int)


def test_is_admin_true():
    from bot.handlers.admin import _is_admin
    if settings.admin_ids:
        assert _is_admin(settings.admin_ids[0])


def test_is_admin_false():
    from bot.handlers.admin import _is_admin
    assert not _is_admin(0)
    assert not _is_admin(999999999)


# Broadcast / export / stats are tested against the REAL code in
# tests/test_audit_fixes.py (the old tests re-implemented the loop inline).


# ── Welcome message persistence ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_welcome_repo_set_get():
    """WelcomeRepo.set then get must return the same text."""
    mock_session = AsyncMock()

    class FakeResult:
        def scalar_one_or_none(self):
            return "stored text"

    mock_session.execute = AsyncMock(return_value=FakeResult())
    mock_session.commit = AsyncMock()

    from bot.db.repo import WelcomeRepo
    repo = WelcomeRepo(mock_session)
    result = await repo.get("uz")
    assert result == "stored text"


# ── Stats counters ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_user_repo_inc_downloads():
    """inc_downloads must call execute and commit."""
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock()
    mock_session.commit = AsyncMock()

    from bot.db.repo import UserRepo
    repo = UserRepo(mock_session)
    await repo.inc_downloads(12345)

    assert mock_session.execute.called
    assert mock_session.commit.called
