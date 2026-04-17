"""Unit tests for the Xanadu Gold monthly membership fee processor."""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import discord

import config
from NightCityBot.cogs.economy import Economy
from NightCityBot.utils import config_loader as _cfg


def _make_member(*, has_xanadu: bool, has_loa: bool = False) -> MagicMock:
    member = MagicMock(spec=discord.Member)
    member.id = 12345
    roles = []
    if has_xanadu:
        r = MagicMock(spec=discord.Role)
        r.id = config.XANADU_GOLD_ROLE_ID
        roles.append(r)
    if has_loa:
        r = MagicMock(spec=discord.Role)
        r.id = config.LOA_ROLE_ID
        roles.append(r)
    member.roles = roles
    return member


def _make_economy() -> Economy:
    bot = MagicMock()
    bot.get_cog = MagicMock(return_value=None)
    bot.unbelievaboat = MagicMock()
    bot.unbelievaboat.update_balance = AsyncMock(return_value=True)
    eco = Economy.__new__(Economy)
    eco.bot = bot
    eco.unbelievaboat = bot.unbelievaboat
    return eco


def test_xanadu_gold_charges_member_with_role():
    eco = _make_economy()
    member = _make_member(has_xanadu=True)
    log: list[str] = []
    rent_log = MagicMock()
    rent_log.send = AsyncMock()

    with patch.dict(_cfg._cache, {}, clear=True):
        cash, bank = asyncio.run(
            eco.process_xanadu_gold(member, 1000, 5000, log, rent_log)
        )

    assert cash == 500
    assert bank == 5000
    eco.unbelievaboat.update_balance.assert_awaited_once()
    payload = eco.unbelievaboat.update_balance.await_args.args[1]
    assert payload == {"cash": -500}
    assert any("Xanadu Gold membership detected" in line for line in log)
    assert any("collected" in line for line in log)
    rent_log.send.assert_awaited_once()


def test_xanadu_gold_skips_member_without_role():
    eco = _make_economy()
    member = _make_member(has_xanadu=False)
    log: list[str] = []

    with patch.dict(_cfg._cache, {}, clear=True):
        cash, bank = asyncio.run(
            eco.process_xanadu_gold(member, 1000, 5000, log, None)
        )

    assert (cash, bank) == (1000, 5000)
    eco.unbelievaboat.update_balance.assert_not_called()
    assert log == []


def test_xanadu_gold_insufficient_funds_does_not_deduct():
    eco = _make_economy()
    member = _make_member(has_xanadu=True)
    log: list[str] = []

    with patch.dict(_cfg._cache, {}, clear=True):
        cash, bank = asyncio.run(
            eco.process_xanadu_gold(member, 100, 200, log, None)
        )

    assert (cash, bank) == (100, 200)
    eco.unbelievaboat.update_balance.assert_not_called()
    assert any("Insufficient funds" in line for line in log)


def test_xanadu_gold_disabled_system_skips():
    eco = _make_economy()
    control = MagicMock()
    control.is_enabled = MagicMock(return_value=False)
    eco.bot.get_cog = MagicMock(return_value=control)
    member = _make_member(has_xanadu=True)
    log: list[str] = []

    cash, bank = asyncio.run(
        eco.process_xanadu_gold(member, 5000, 5000, log, None)
    )

    assert (cash, bank) == (5000, 5000)
    eco.unbelievaboat.update_balance.assert_not_called()
    assert any("Xanadu Gold system disabled" in line for line in log)


def test_xanadu_gold_dry_run_does_not_call_api():
    eco = _make_economy()
    member = _make_member(has_xanadu=True)
    log: list[str] = []

    with patch.dict(_cfg._cache, {}, clear=True):
        cash, bank = asyncio.run(
            eco.process_xanadu_gold(member, 1000, 5000, log, None, dry_run=True)
        )

    assert cash == 500
    assert bank == 5000
    eco.unbelievaboat.update_balance.assert_not_called()
    assert any("Would subtract" in line for line in log)


def test_xanadu_gold_zero_cost_no_op():
    eco = _make_economy()
    member = _make_member(has_xanadu=True)
    log: list[str] = []

    with patch.dict(_cfg._cache, {"xanadu_gold_cost": "0"}, clear=True):
        cash, bank = asyncio.run(
            eco.process_xanadu_gold(member, 1000, 5000, log, None)
        )

    assert (cash, bank) == (1000, 5000)
    eco.unbelievaboat.update_balance.assert_not_called()


def test_xanadu_gold_splits_across_cash_and_bank():
    """When cash < cost, deduct what's available from cash and the rest from bank."""
    eco = _make_economy()
    member = _make_member(has_xanadu=True)
    log: list[str] = []

    with patch.dict(_cfg._cache, {}, clear=True):
        cash, bank = asyncio.run(
            eco.process_xanadu_gold(member, 100, 5000, log, None)
        )

    assert cash == 0
    assert bank == 4600
    payload = eco.unbelievaboat.update_balance.await_args.args[1]
    assert payload == {"cash": -100, "bank": -400}
