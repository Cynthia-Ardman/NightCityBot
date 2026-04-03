import asyncio
from unittest.mock import AsyncMock, MagicMock

import discord
from discord.ext import commands
import pytest

import config


def _run(coro):
    return asyncio.run(coro)


def _make_role(role_id):
    r = MagicMock(spec=discord.Role)
    r.id = role_id
    return r


def _guild_with_member(member):
    guild = MagicMock(spec=discord.Guild)
    guild.get_member = MagicMock(return_value=member)
    return guild


def _guild_fetch_member(member=None, *, not_found=False):
    guild = MagicMock(spec=discord.Guild)
    guild.get_member = MagicMock(return_value=None)
    if not_found:
        guild.fetch_member = AsyncMock(side_effect=discord.NotFound(MagicMock(), "nope"))
    else:
        guild.fetch_member = AsyncMock(return_value=member)
    return guild


def _ctx_in_guild(roles):
    ctx = MagicMock()
    member = MagicMock(spec=discord.Member)
    member.roles = roles
    member.id = 1
    ctx.author = member
    return ctx


def _ctx_in_dm(guild):
    ctx = MagicMock()
    user = MagicMock(spec=discord.User)
    user.id = 1
    ctx.author = user
    ctx.bot = MagicMock()
    ctx.bot.get_guild = MagicMock(return_value=guild)
    return ctx


class TestIsFixer:
    def _predicate(self):
        from NightCityBot.utils.permissions import is_fixer
        dec = is_fixer()
        return dec.predicate

    def test_passes_in_guild(self):
        pred = self._predicate()
        ctx = _ctx_in_guild([_make_role(config.FIXER_ROLE_ID)])
        assert _run(pred(ctx)) is True

    def test_fails_in_guild(self):
        pred = self._predicate()
        ctx = _ctx_in_guild([_make_role(99999)])
        with pytest.raises(commands.CheckFailure):
            _run(pred(ctx))

    def test_dm_fetch_success(self):
        pred = self._predicate()
        member = MagicMock(spec=discord.Member)
        member.roles = [_make_role(config.FIXER_ROLE_ID)]
        guild = _guild_fetch_member(member)
        ctx = _ctx_in_dm(guild)
        assert _run(pred(ctx)) is True

    def test_dm_no_guild(self):
        pred = self._predicate()
        ctx = _ctx_in_dm(None)
        with pytest.raises(commands.CheckFailure):
            _run(pred(ctx))

    def test_dm_not_found(self):
        pred = self._predicate()
        guild = _guild_fetch_member(not_found=True)
        ctx = _ctx_in_dm(guild)
        with pytest.raises(commands.CheckFailure):
            _run(pred(ctx))

    def test_dm_cached_member(self):
        pred = self._predicate()
        member = MagicMock(spec=discord.Member)
        member.roles = [_make_role(config.FIXER_ROLE_ID)]
        guild = _guild_with_member(member)
        ctx = _ctx_in_dm(guild)
        assert _run(pred(ctx)) is True


class TestIsRipperdoc:
    def _predicate(self):
        from NightCityBot.utils.permissions import is_ripperdoc
        return is_ripperdoc().predicate

    def test_passes_in_guild(self):
        pred = self._predicate()
        ctx = _ctx_in_guild([_make_role(config.RIPPERDOC_ROLE_ID)])
        assert _run(pred(ctx)) is True

    def test_fails_in_guild(self):
        pred = self._predicate()
        ctx = _ctx_in_guild([_make_role(99999)])
        with pytest.raises(commands.CheckFailure):
            _run(pred(ctx))

    def test_dm_fetch(self):
        pred = self._predicate()
        member = MagicMock(spec=discord.Member)
        member.roles = [_make_role(config.RIPPERDOC_ROLE_ID)]
        guild = _guild_fetch_member(member)
        ctx = _ctx_in_dm(guild)
        assert _run(pred(ctx)) is True

    def test_dm_no_guild(self):
        pred = self._predicate()
        ctx = _ctx_in_dm(None)
        with pytest.raises(commands.CheckFailure):
            _run(pred(ctx))


class TestIsCsApprover:
    def _predicate(self):
        from NightCityBot.utils.permissions import is_cs_approver
        return is_cs_approver().predicate

    def test_passes_in_guild(self):
        pred = self._predicate()
        ctx = _ctx_in_guild([_make_role(config.CS_APPROVER_ROLE_ID)])
        assert _run(pred(ctx)) is True

    def test_fails_in_guild(self):
        pred = self._predicate()
        ctx = _ctx_in_guild([_make_role(99999)])
        with pytest.raises(commands.CheckFailure):
            _run(pred(ctx))

    def test_dm_no_guild(self):
        pred = self._predicate()
        ctx = _ctx_in_dm(None)
        with pytest.raises(commands.CheckFailure):
            _run(pred(ctx))

    def test_dm_not_found(self):
        pred = self._predicate()
        guild = _guild_fetch_member(not_found=True)
        ctx = _ctx_in_dm(guild)
        with pytest.raises(commands.CheckFailure):
            _run(pred(ctx))


class TestIsStoreOwner:
    def _predicate(self):
        from NightCityBot.utils.permissions import is_store_owner
        return is_store_owner().predicate

    def _store_role_id(self):
        raw = config.WHOLESALER_STORE_ROLE_IDS
        if isinstance(raw, (int, float, str)) and str(raw).strip().isdigit():
            return int(raw)
        return int(list(raw)[0])

    def test_passes_in_guild(self):
        pred = self._predicate()
        ctx = _ctx_in_guild([_make_role(self._store_role_id())])
        assert _run(pred(ctx)) is True

    def test_fails_in_guild(self):
        pred = self._predicate()
        ctx = _ctx_in_guild([_make_role(99999)])
        with pytest.raises(commands.CheckFailure):
            _run(pred(ctx))

    def test_dm_no_guild(self):
        pred = self._predicate()
        ctx = _ctx_in_dm(None)
        with pytest.raises(commands.CheckFailure):
            _run(pred(ctx))
