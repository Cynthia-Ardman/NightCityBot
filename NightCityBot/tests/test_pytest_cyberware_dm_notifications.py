"""Tests for the user-facing DM notifications sent during the weekly cyberware run."""
import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import discord

import config
from NightCityBot.cogs.cyberware import CyberwareManager


def _make_role(rid: int, name: str) -> MagicMock:
    r = MagicMock(spec=discord.Role)
    r.id = rid
    r.name = name
    return r


def _make_manager() -> CyberwareManager:
    """Construct a CyberwareManager without invoking the constructor side-effects."""
    bot = MagicMock()
    bot.unbelievaboat = MagicMock()
    bot.unbelievaboat.get_balance = AsyncMock(return_value={"cash": 0, "bank": 100000})
    bot.unbelievaboat.update_balance = AsyncMock(return_value=True)
    mgr = CyberwareManager.__new__(CyberwareManager)
    mgr.bot = bot
    mgr.unbelievaboat = bot.unbelievaboat
    mgr.data = {}
    mgr.last_run = None
    return mgr


def _make_member(*, has_checkup: bool, level_role_id: int) -> MagicMock:
    member = MagicMock(spec=discord.Member)
    member.id = 999
    member.send = AsyncMock()
    roles = [
        _make_role(config.APPROVED_ROLE_ID, "Approved Character"),
        _make_role(level_role_id, "Cyberware Level"),
    ]
    if has_checkup:
        roles.append(_make_role(config.CYBER_CHECKUP_ROLE_ID, "Cyberware Checkup"))
    member.roles = roles
    member.add_roles = AsyncMock()
    return member


def test_charged_dm_includes_amount_and_breakdown():
    mgr = _make_manager()
    member = _make_member(has_checkup=True, level_role_id=config.CYBER_MEDIUM_ROLE_ID)

    asyncio.run(mgr._notify_member_charged(member, 1500, 3, "medium", 200, 1300))

    member.send.assert_awaited_once()
    msg = member.send.await_args.args[0]
    assert "$1,500" in msg
    assert "medium" in msg
    assert "week 3" in msg
    assert "$200" in msg and "from cash" in msg
    assert "$1,300" in msg and "from bank" in msg


def test_charged_dm_omits_zero_buckets():
    mgr = _make_manager()
    member = _make_member(has_checkup=True, level_role_id=config.CYBER_HIGH_ROLE_ID)

    asyncio.run(mgr._notify_member_charged(member, 500, 1, "high", 500, 0))

    msg = member.send.await_args.args[0]
    assert "$500" in msg
    assert "from cash" in msg
    assert "from bank" not in msg


def test_payment_failed_dm_includes_amount():
    mgr = _make_manager()
    member = _make_member(has_checkup=True, level_role_id=config.CYBER_EXTREME_ROLE_ID)

    asyncio.run(mgr._notify_member_payment_failed(member, 8000, 4, "extreme"))

    msg = member.send.await_args.args[0]
    assert "$8,000" in msg
    assert "extreme" in msg
    assert "week 4" in msg
    assert "Failed" in msg or "failed" in msg


def test_checkup_due_dm_no_amount():
    mgr = _make_manager()
    member = _make_member(has_checkup=False, level_role_id=config.CYBER_MEDIUM_ROLE_ID)

    asyncio.run(mgr._notify_member_checkup_due(member))

    msg = member.send.await_args.args[0]
    assert "Checkup Due" in msg
    assert "No money was deducted" in msg


def test_dm_failures_are_swallowed():
    """If the user has DMs disabled, the helpers must not raise."""
    mgr = _make_manager()
    member = _make_member(has_checkup=True, level_role_id=config.CYBER_MEDIUM_ROLE_ID)
    member.send.side_effect = discord.Forbidden(MagicMock(status=403, reason="x"), "blocked")

    asyncio.run(mgr._notify_member_charged(member, 100, 1, "medium", 100, 0))
    asyncio.run(mgr._notify_member_payment_failed(member, 100, 1, "medium"))
    asyncio.run(mgr._notify_member_checkup_due(member))


def test_process_week_dms_user_on_successful_charge():
    """When a member is charged, process_week must DM them with the amount."""
    mgr = _make_manager()
    member = _make_member(has_checkup=True, level_role_id=config.CYBER_MEDIUM_ROLE_ID)

    guild = MagicMock()
    role_map = {
        config.CYBER_CHECKUP_ROLE_ID: _make_role(config.CYBER_CHECKUP_ROLE_ID, "checkup"),
        config.CYBER_MEDIUM_ROLE_ID: _make_role(config.CYBER_MEDIUM_ROLE_ID, "medium"),
        config.CYBER_HIGH_ROLE_ID: _make_role(config.CYBER_HIGH_ROLE_ID, "high"),
        config.CYBER_EXTREME_ROLE_ID: _make_role(config.CYBER_EXTREME_ROLE_ID, "extreme"),
        config.LOA_ROLE_ID: _make_role(config.LOA_ROLE_ID, "loa"),
        config.RIPPERDOC_ROLE_ID: _make_role(config.RIPPERDOC_ROLE_ID, "ripperdoc"),
    }
    # Member has the checkup role, so we must include the actual checkup role obj on them.
    checkup_role_obj = role_map[config.CYBER_CHECKUP_ROLE_ID]
    member.roles = [
        _make_role(config.APPROVED_ROLE_ID, "Approved Character"),
        role_map[config.CYBER_MEDIUM_ROLE_ID],
        checkup_role_obj,
    ]
    guild.get_role.side_effect = lambda rid: role_map.get(rid)
    guild.get_channel.return_value = None
    guild.members = [member]

    mgr.bot.get_guild.return_value = guild
    mgr.bot.get_cog.return_value = None
    # Make sure it's been at least 1 week since the last run so weeks increments to 1
    mgr.last_run = None

    with patch("NightCityBot.cogs.cyberware.cyberware_status_upsert_many", new=AsyncMock(return_value=True)), \
         patch("NightCityBot.cogs.cyberware.cyberware_last_run_set", new=AsyncMock(return_value=True)):
        results = asyncio.run(mgr.process_week(target_member=member))

    assert member.id in results["paid"]
    member.send.assert_awaited()  # at least once
    msg = member.send.await_args.args[0]
    assert "Cyberware Medication Charged" in msg
    assert "$" in msg


def test_process_week_dms_user_on_payment_failure():
    """When a member can't pay, process_week must DM them about the failure."""
    mgr = _make_manager()
    mgr.unbelievaboat.get_balance = AsyncMock(return_value={"cash": 0, "bank": 0})
    member = _make_member(has_checkup=True, level_role_id=config.CYBER_MEDIUM_ROLE_ID)

    guild = MagicMock()
    role_map = {
        config.CYBER_CHECKUP_ROLE_ID: _make_role(config.CYBER_CHECKUP_ROLE_ID, "checkup"),
        config.CYBER_MEDIUM_ROLE_ID: _make_role(config.CYBER_MEDIUM_ROLE_ID, "medium"),
        config.CYBER_HIGH_ROLE_ID: _make_role(config.CYBER_HIGH_ROLE_ID, "high"),
        config.CYBER_EXTREME_ROLE_ID: _make_role(config.CYBER_EXTREME_ROLE_ID, "extreme"),
        config.LOA_ROLE_ID: _make_role(config.LOA_ROLE_ID, "loa"),
        config.RIPPERDOC_ROLE_ID: _make_role(config.RIPPERDOC_ROLE_ID, "ripperdoc"),
    }
    checkup_role_obj = role_map[config.CYBER_CHECKUP_ROLE_ID]
    member.roles = [
        _make_role(config.APPROVED_ROLE_ID, "Approved Character"),
        role_map[config.CYBER_MEDIUM_ROLE_ID],
        checkup_role_obj,
    ]
    guild.get_role.side_effect = lambda rid: role_map.get(rid)
    guild.get_channel.return_value = None
    guild.members = [member]

    mgr.bot.get_guild.return_value = guild
    mgr.bot.get_cog.return_value = None

    with patch("NightCityBot.cogs.cyberware.cyberware_status_upsert_many", new=AsyncMock(return_value=True)), \
         patch("NightCityBot.cogs.cyberware.cyberware_last_run_set", new=AsyncMock(return_value=True)):
        results = asyncio.run(mgr.process_week(target_member=member))

    assert member.id in results["unpaid"]
    member.send.assert_awaited()
    msg = member.send.await_args.args[0]
    assert "Payment Failed" in msg


def test_process_week_dms_user_on_first_checkup_assignment():
    """When a member is given the checkup role for the first time, DM them."""
    mgr = _make_manager()
    member = _make_member(has_checkup=False, level_role_id=config.CYBER_MEDIUM_ROLE_ID)

    guild = MagicMock()
    role_map = {
        config.CYBER_CHECKUP_ROLE_ID: _make_role(config.CYBER_CHECKUP_ROLE_ID, "checkup"),
        config.CYBER_MEDIUM_ROLE_ID: _make_role(config.CYBER_MEDIUM_ROLE_ID, "medium"),
        config.CYBER_HIGH_ROLE_ID: _make_role(config.CYBER_HIGH_ROLE_ID, "high"),
        config.CYBER_EXTREME_ROLE_ID: _make_role(config.CYBER_EXTREME_ROLE_ID, "extreme"),
        config.LOA_ROLE_ID: _make_role(config.LOA_ROLE_ID, "loa"),
        config.RIPPERDOC_ROLE_ID: _make_role(config.RIPPERDOC_ROLE_ID, "ripperdoc"),
    }
    member.roles = [
        _make_role(config.APPROVED_ROLE_ID, "Approved Character"),
        role_map[config.CYBER_MEDIUM_ROLE_ID],
    ]
    guild.get_role.side_effect = lambda rid: role_map.get(rid)
    guild.get_channel.return_value = None
    guild.members = [member]

    mgr.bot.get_guild.return_value = guild
    mgr.bot.get_cog.return_value = None

    with patch("NightCityBot.cogs.cyberware.cyberware_status_upsert_many", new=AsyncMock(return_value=True)), \
         patch("NightCityBot.cogs.cyberware.cyberware_last_run_set", new=AsyncMock(return_value=True)):
        results = asyncio.run(mgr.process_week(target_member=member))

    assert member.id in results["checkup"]
    member.send.assert_awaited()
    msg = member.send.await_args.args[0]
    assert "Checkup Due" in msg
    # No deduction message should have been sent
    assert "Charged" not in msg
