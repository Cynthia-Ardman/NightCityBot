"""Tests for the weekly cyberware summary embed posted to the cyberware-logs channel."""
import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import discord

import config
from NightCityBot.cogs.cyberware import CyberwareManager


def _make_manager() -> CyberwareManager:
    bot = MagicMock()
    bot.unbelievaboat = MagicMock()
    mgr = CyberwareManager.__new__(CyberwareManager)
    mgr.bot = bot
    mgr.unbelievaboat = bot.unbelievaboat
    mgr.data = {}
    mgr.last_run = None
    return mgr


def _sample_results() -> dict:
    return {
        "checkup": [111],
        "paid": [222, 333],
        "unpaid": [444],
        "details": {
            "checkup": [{"id": 111, "level": "medium"}],
            "paid": [
                {"id": 222, "cost": 1500, "weeks": 2, "level": "medium",
                 "cash": 500, "bank": 1000},
                {"id": 333, "cost": 8000, "weeks": 4, "level": "extreme",
                 "cash": 0, "bank": 8000},
            ],
            "unpaid": [
                {"id": 444, "cost": 5000, "weeks": 3, "level": "high"},
            ],
        },
    }


def test_summary_embed_includes_totals_and_breakdown():
    mgr = _make_manager()
    embed = mgr._build_weekly_summary_embed(_sample_results(), datetime.now(timezone.utc))

    assert "Weekly Cyberware Run" in embed.title
    desc = embed.description or ""
    # Charged: 2 totaling $9,500
    assert "Charged:" in desc and "2" in desc and "$9,500" in desc
    # Payment Failed: 1 totaling $5,000
    assert "Payment Failed:" in desc and "1" in desc and "$5,000" in desc
    # Checkup notices: 1
    assert "Checkup Notices:" in desc and "1" in desc

    field_names = [f.name for f in embed.fields]
    field_values = "\n".join(f.value for f in embed.fields)
    assert any("Charged" in n for n in field_names)
    assert any("Payment Failed" in n for n in field_names)
    assert any("Checkup Notices" in n for n in field_names)
    assert "<@222>" in field_values and "$1,500" in field_values and "medium" in field_values
    assert "<@333>" in field_values and "$8,000" in field_values and "extreme" in field_values
    assert "<@444>" in field_values and "$5,000" in field_values and "high" in field_values
    assert "<@111>" in field_values


def test_summary_embed_handles_empty_results():
    mgr = _make_manager()
    empty = {"checkup": [], "paid": [], "unpaid": [],
             "details": {"checkup": [], "paid": [], "unpaid": []}}
    embed = mgr._build_weekly_summary_embed(empty, datetime.now(timezone.utc))

    assert any("No members required action" in (f.value or "") for f in embed.fields)


def test_summary_embed_chunks_long_charged_lists():
    mgr = _make_manager()
    paid = [
        {"id": 1000000000000000000 + i, "cost": 9999, "weeks": 9, "level": "extreme",
         "cash": 0, "bank": 9999}
        for i in range(80)
    ]
    results = {
        "checkup": [], "paid": [e["id"] for e in paid], "unpaid": [],
        "details": {"checkup": [], "paid": paid, "unpaid": []},
    }
    embed = mgr._build_weekly_summary_embed(results, datetime.now(timezone.utc))

    charged_fields = [f for f in embed.fields if "Charged" in f.name]
    assert len(charged_fields) >= 2  # forced into multiple chunks
    for f in charged_fields:
        assert len(f.value) <= 1024


def test_summary_embed_caps_per_category_and_appends_more_tail():
    """Even with a huge list, no category should exceed 7 fields and the embed
    must respect Discord's 25-field overall cap."""
    mgr = _make_manager()
    paid = [
        {"id": 1000000000000000000 + i, "cost": 9999, "weeks": 9, "level": "extreme",
         "cash": 0, "bank": 9999}
        for i in range(2000)
    ]
    results = {
        "checkup": [], "paid": [e["id"] for e in paid], "unpaid": [],
        "details": {"checkup": [], "paid": paid, "unpaid": []},
    }
    embed = mgr._build_weekly_summary_embed(results, datetime.now(timezone.utc))

    charged_fields = [f for f in embed.fields if "Charged" in f.name]
    assert len(charged_fields) <= 7
    assert len(embed.fields) <= 25
    for f in embed.fields:
        assert len(f.value) <= 1024
    # The truncation tail must be present
    assert any("more" in (f.value or "") for f in charged_fields)


def test_summary_embed_works_when_details_key_missing():
    """If process_week early-returned without details (legacy callers), the
    summary builder must not crash."""
    mgr = _make_manager()
    legacy = {"checkup": [], "paid": [], "unpaid": []}
    embed = mgr._build_weekly_summary_embed(legacy, datetime.now(timezone.utc))
    assert embed.title is not None
    assert any("No members" in (f.value or "") for f in embed.fields)


def test_post_weekly_summary_sends_to_cyberware_log_channel(monkeypatch):
    monkeypatch.setattr("config.CYBERWARE_LOG_CHANNEL_ID", 424242)

    mgr = _make_manager()
    log_ch = MagicMock(spec=discord.TextChannel)
    log_ch.send = AsyncMock()
    guild = MagicMock()
    guild.get_channel = MagicMock(return_value=log_ch)
    mgr.bot.get_guild = MagicMock(return_value=guild)

    asyncio.run(mgr._post_weekly_summary(_sample_results(), datetime.now(timezone.utc)))

    log_ch.send.assert_awaited_once()
    sent_embed = log_ch.send.await_args.kwargs.get("embed")
    assert sent_embed is not None
    assert "Weekly Cyberware Run" in sent_embed.title


def test_post_weekly_summary_skipped_when_channel_id_zero(monkeypatch):
    monkeypatch.setattr("config.CYBERWARE_LOG_CHANNEL_ID", 0)

    mgr = _make_manager()
    mgr.bot.get_guild = MagicMock()
    mgr.bot.fetch_channel = AsyncMock()

    asyncio.run(mgr._post_weekly_summary(_sample_results(), datetime.now(timezone.utc)))

    mgr.bot.get_guild.assert_not_called()
    mgr.bot.fetch_channel.assert_not_called()


def test_post_weekly_summary_falls_back_to_fetch_channel(monkeypatch):
    monkeypatch.setattr("config.CYBERWARE_LOG_CHANNEL_ID", 424242)

    mgr = _make_manager()
    log_ch = MagicMock(spec=discord.TextChannel)
    log_ch.send = AsyncMock()
    mgr.bot.get_guild = MagicMock(return_value=None)
    mgr.bot.fetch_channel = AsyncMock(return_value=log_ch)

    asyncio.run(mgr._post_weekly_summary(_sample_results(), datetime.now(timezone.utc)))

    mgr.bot.fetch_channel.assert_awaited_once_with(424242)
    log_ch.send.assert_awaited_once()
