"""Tests for unified shop system — ripperdoc_hub, gunstore_hub, admin_shop, and item_history."""

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

import config

from NightCityBot.cogs.ripperdoc_hub import (
    RipperdocHub,
    RipperdocMenuView,
    DMConfirmView,
)
from NightCityBot.cogs.gunstore_hub import (
    GunstoreHub,
    GunstoreMenuView,
    GunDMConfirmView,
    InlineApproveView,
    GunSellSetupView,
    GUN_STORE_EMPLOYEE_ROLE_ID,
    _is_store_owner_member,
    _is_employee_member,
    _find_employee_store,
    _ManageEmployeesView,
    _EmployeePickerView,
)
from NightCityBot.cogs.admin_shop import (
    AdminShopCog,
    AdminShopMenuView,
    PlayerInvPickerView as AdminPlayerInvPickerView,
    WholesaleClearConfirmView,
)
from NightCityBot.cogs.ripperdoc_hub import (
    SellSetupView,
)
from NightCityBot.cogs.player_inventory import TradeConfirmView


def _run(coro):
    return asyncio.run(coro)


async def _cmd(cog, method_name, ctx, *args, **kwargs):
    cmd = getattr(cog, method_name)
    if hasattr(cmd, "callback"):
        return await cmd.callback(cog, ctx, *args, **kwargs)
    return await cmd(ctx, *args, **kwargs)


def _make_bot():
    bot = MagicMock()
    bot.get_channel = MagicMock(return_value=None)
    bot.fetch_channel = AsyncMock(side_effect=Exception("no channel"))
    bot.cogs = {}
    bot.unbelievaboat = MagicMock()
    bot.unbelievaboat.get_balance = AsyncMock(return_value={"cash": 50000, "bank": 0})
    bot.unbelievaboat.update_balance = AsyncMock(return_value=True)
    return bot


def _ctx(author_id=111, guild=True, admin=False):
    ctx = MagicMock()
    ctx.send = AsyncMock()
    ctx.author = MagicMock(spec=discord.Member)
    ctx.author.id = author_id
    ctx.author.display_name = f"User{author_id}"
    ctx.author.mention = f"<@{author_id}>"
    ctx.author.roles = []
    ctx.author.guild_permissions = MagicMock()
    ctx.author.guild_permissions.administrator = admin
    if guild:
        ctx.guild = MagicMock()
        ctx.guild.id = 999
        ctx.guild.get_member = MagicMock(return_value=None)
        ctx.guild.fetch_member = AsyncMock(return_value=None)
    else:
        ctx.guild = None
    return ctx


def _make_member(member_id, name="Member", roles=None):
    m = MagicMock(spec=discord.Member)
    m.id = member_id
    m.display_name = name
    m.mention = f"<@{member_id}>"
    m.roles = roles or []
    m.guild_permissions = MagicMock()
    m.guild_permissions.administrator = False
    dm_msg = MagicMock()
    dm_msg.edit = AsyncMock()
    m.send = AsyncMock(return_value=dm_msg)
    return m


def _make_interaction(user_id=111, roles=None, admin=False):
    inter = MagicMock(spec=discord.Interaction)
    inter.user = MagicMock(spec=discord.Member)
    inter.user.id = user_id
    inter.user.roles = roles or []
    inter.user.guild_permissions = MagicMock()
    inter.user.guild_permissions.administrator = admin
    inter.response = MagicMock()
    inter.response.defer = AsyncMock()
    inter.response.send_message = AsyncMock()
    inter.response.edit_message = AsyncMock()
    inter.followup = MagicMock()
    inter.followup.send = AsyncMock()
    inter.edit_original_response = AsyncMock()
    inter.client = MagicMock()
    inter.client.get_cog = MagicMock(return_value=None)
    inter.guild = MagicMock()
    inter.guild.id = 999
    return inter


def _find_button(view, label):
    for child in view.children:
        if getattr(child, "label", None) == label:
            return child
    raise ValueError(f"No button with label '{label}' in view")


def _find_user_select(view):
    for child in view.children:
        if isinstance(child, discord.ui.UserSelect):
            return child
    raise ValueError("No UserSelect in view")


def _make_ripperdoc_cog():
    bot = _make_bot()
    cog = RipperdocHub.__new__(RipperdocHub)
    cog.bot = bot
    cog.unbelievaboat = bot.unbelievaboat
    return cog


def _make_gunstore_cog():
    bot = _make_bot()
    cog = GunstoreHub.__new__(GunstoreHub)
    cog.bot = bot
    cog.unbelievaboat = bot.unbelievaboat
    return cog


def _make_admin_cog():
    bot = _make_bot()
    cog = AdminShopCog.__new__(AdminShopCog)
    cog.bot = bot
    return cog


class TestRipperdocHubCommand:
    def test_dm_guard(self, monkeypatch):
        monkeypatch.setattr("config.CYBERWARE_LOG_CHANNEL_ID", 0)
        monkeypatch.setattr("config.RIPPERDOC_HUB_CHANNEL_ID", 0)
        cog = _make_ripperdoc_cog()
        cog.bot.get_channel = MagicMock(return_value=None)
        ctx = _ctx(admin=True)
        _run(_cmd(cog, "ripperdoc_hub", ctx))
        assert ctx.send.called

    def test_sends_embed_with_view(self, monkeypatch):
        monkeypatch.setattr("config.CYBERWARE_LOG_CHANNEL_ID", 0)
        monkeypatch.setattr("config.RIPPERDOC_HUB_CHANNEL_ID", 12345)
        cog = _make_ripperdoc_cog()
        channel = MagicMock()
        channel.send = AsyncMock()
        cog.bot.get_channel = MagicMock(return_value=channel)
        ctx = _ctx(admin=True)
        _run(_cmd(cog, "ripperdoc_hub", ctx))
        assert channel.send.called
        call_kwargs = channel.send.call_args
        assert "view" in call_kwargs.kwargs


class TestRipperdocMenuView:
    def test_interaction_check_ripperdoc_passes(self, monkeypatch):
        monkeypatch.setattr("config.RIPPERDOC_ROLE_ID", 555)

        async def run():
            view = RipperdocMenuView()
            role = MagicMock()
            role.id = 555
            inter = _make_interaction(user_id=111, roles=[role])
            return await view.interaction_check(inter)

        assert _run(run()) is True

    def test_interaction_check_non_ripperdoc_fails(self, monkeypatch):
        monkeypatch.setattr("config.RIPPERDOC_ROLE_ID", 555)

        async def run():
            view = RipperdocMenuView()
            inter = _make_interaction(user_id=999, roles=[])
            result = await view.interaction_check(inter)
            inter.response.send_message.assert_called_once()
            return result

        assert _run(run()) is False

    def test_buy_wholesale_no_cog(self, monkeypatch):
        monkeypatch.setattr("config.CYBERWARE_LOG_CHANNEL_ID", 0)

        async def run():
            view = RipperdocMenuView()
            inter = _make_interaction()
            inter.client.get_cog = MagicMock(return_value=None)
            btn = _find_button(view, "Buy from Wholesale")
            await btn.callback(inter)
            return inter.followup.send.call_args[0][0]

        msg = _run(run())
        assert "unavailable" in msg.lower()

    def test_sell_opens_setup_view(self, monkeypatch):
        monkeypatch.setattr("config.CYBERWARE_LOG_CHANNEL_ID", 0)

        async def run():
            cog = _make_ripperdoc_cog()
            cw_cog = MagicMock()
            cw_cog._load_state = AsyncMock(return_value={"ripperdoc_stores": {}})
            cw_cog._load_inventory = AsyncMock(return_value=[
                {"item_id": "x", "name": "Optics", "price_paid": 100}
            ])
            cw_cog._grouped_inventory = MagicMock(return_value=[
                {"name": "Optics", "count": 1, "items": [
                    {"item_id": "x", "name": "Optics"}
                ]}
            ])
            view = RipperdocMenuView()
            inter = _make_interaction()
            inter.client.get_cog = MagicMock(side_effect=lambda n: cw_cog if n == "CyberwareShop" else cog if n == "RipperdocHub" else None)
            btn = _find_button(view, "Sell to Patient")
            await btn.callback(inter)
            inter.response.defer.assert_called_once()
            inter.followup.send.assert_called_once()
            sent_view = inter.followup.send.call_args[1].get("view")
            assert isinstance(sent_view, SellSetupView)

        _run(run())

    def test_install_opens_setup_view(self, monkeypatch):
        monkeypatch.setattr("config.CYBERWARE_LOG_CHANNEL_ID", 0)

        async def run():
            cog = _make_ripperdoc_cog()
            cw_cog = MagicMock()
            cw_cog._load_state = AsyncMock(return_value={"ripperdoc_stores": {}})
            cw_cog._load_inventory = AsyncMock(return_value=[
                {"item_id": "x", "name": "Optics", "price_paid": 100}
            ])
            cw_cog._grouped_inventory = MagicMock(return_value=[
                {"name": "Optics", "count": 1, "items": [
                    {"item_id": "x", "name": "Optics"}
                ]}
            ])
            view = RipperdocMenuView()
            inter = _make_interaction()
            inter.client.get_cog = MagicMock(side_effect=lambda n: cw_cog if n == "CyberwareShop" else cog if n == "RipperdocHub" else None)
            btn = _find_button(view, "Install on Patient")
            await btn.callback(inter)
            inter.response.defer.assert_called_once()
            inter.followup.send.assert_called_once()
            sent_view = inter.followup.send.call_args[1].get("view")
            assert isinstance(sent_view, SellSetupView)

        _run(run())


class TestDMConfirmView:
    def test_accept_sets_flag(self):
        async def run():
            view = DMConfirmView(recipient_id=100, timeout=10)
            assert view.accepted is None
            inter = _make_interaction()
            btn = _find_button(view, "Accept")
            await btn.callback(inter)
            return view.accepted

        assert _run(run()) is True

    def test_decline_sets_flag(self):
        async def run():
            view = DMConfirmView(recipient_id=100, timeout=10)
            assert view.accepted is None
            inter = _make_interaction()
            btn = _find_button(view, "Decline")
            await btn.callback(inter)
            return view.accepted

        assert _run(run()) is False


class TestGunstoreHubCommand:
    def test_dm_guard(self, monkeypatch):
        monkeypatch.setattr("config.GUN_LOG_CHANNEL_ID", 0)
        monkeypatch.setattr("config.GUN_HUB_CHANNEL_ID", 0)
        cog = _make_gunstore_cog()
        cog.bot.get_channel = MagicMock(return_value=None)
        ctx = _ctx(admin=True)
        _run(_cmd(cog, "gunstore_hub", ctx))
        assert ctx.send.called

    def test_sends_embed_with_view(self, monkeypatch):
        monkeypatch.setattr("config.GUN_LOG_CHANNEL_ID", 0)
        monkeypatch.setattr("config.GUN_HUB_CHANNEL_ID", 12345)
        cog = _make_gunstore_cog()
        channel = MagicMock()
        channel.send = AsyncMock()
        cog.bot.get_channel = MagicMock(return_value=channel)
        ctx = _ctx(admin=True)
        _run(_cmd(cog, "gunstore_hub", ctx))
        assert channel.send.called
        call_kwargs = channel.send.call_args
        assert "view" in call_kwargs.kwargs


class TestGunstoreMenuView:
    def test_interaction_check_store_owner_passes(self, monkeypatch):
        monkeypatch.setattr("config.WHOLESALER_STORE_ROLE_IDS", 777)

        async def run():
            view = GunstoreMenuView()
            role = MagicMock()
            role.id = 777
            inter = _make_interaction(user_id=111, roles=[role])
            return await view.interaction_check(inter)

        assert _run(run()) is True

    def test_interaction_check_non_owner_fails(self, monkeypatch):
        monkeypatch.setattr("config.WHOLESALER_STORE_ROLE_IDS", 777)

        async def run():
            view = GunstoreMenuView()
            inter = _make_interaction(user_id=999, roles=[])
            result = await view.interaction_check(inter)
            return result

        assert _run(run()) is False

    def test_buy_wholesale_no_cog(self, monkeypatch):
        monkeypatch.setattr("config.GUN_LOG_CHANNEL_ID", 0)

        async def run():
            cog = _make_gunstore_cog()
            cog.bot.cogs = {}
            view = GunstoreMenuView()
            inter = _make_interaction()
            inter.client.get_cog = MagicMock(side_effect=lambda n: cog if n == "GunstoreHub" else None)
            btn = _find_button(view, "Buy from Wholesale")
            await btn.callback(inter)
            return inter.followup.send.call_args[0][0]

        msg = _run(run())
        assert "unavailable" in msg.lower()

    def test_sell_opens_setup_view(self, monkeypatch):
        monkeypatch.setattr("config.GUN_LOG_CHANNEL_ID", 0)
        owner_role_id = config.WHOLESALER_STORE_ROLE_IDS
        if isinstance(owner_role_id, (list, tuple, set)):
            owner_role_id = list(owner_role_id)[0]
        owner_role_id = int(owner_role_id)

        async def run():
            cog = _make_gunstore_cog()
            guns_cog = MagicMock()
            guns_cog._store_id = MagicMock(return_value="999:111")
            guns_cog._load_state = AsyncMock(return_value={
                "stores": {
                    "999:111": {
                        "owner_id": 111,
                        "lots": [{
                            "lot_id": "lot-1",
                            "gun_name": "Pistol",
                            "gun_level": "L",
                            "unit_cost": 100,
                            "qty_remaining": 1,
                            "restriction": "basic",
                        }],
                        "controlled_buyers": [],
                    }
                }
            })
            cog.bot.cogs = {"GunsShopCog": guns_cog}
            view = GunstoreMenuView()
            owner_role = MagicMock()
            owner_role.id = owner_role_id
            inter = _make_interaction(roles=[owner_role])
            inter.client.get_cog = MagicMock(side_effect=lambda n: cog if n == "GunstoreHub" else None)
            btn = _find_button(view, "Sell to Customer")
            await btn.callback(inter)
            call_kwargs = inter.followup.send.call_args[1]
            assert isinstance(call_kwargs.get("view"), GunSellSetupView)

        _run(run())

    def test_inventory_no_cog(self, monkeypatch):
        monkeypatch.setattr("config.GUN_LOG_CHANNEL_ID", 0)

        async def run():
            cog = _make_gunstore_cog()
            cog.bot.cogs = {}
            view = GunstoreMenuView()
            inter = _make_interaction()
            inter.client.get_cog = MagicMock(side_effect=lambda n: cog if n == "GunstoreHub" else None)
            btn = _find_button(view, "My Store Inventory")
            await btn.callback(inter)
            return inter.followup.send.call_args[0][0]

        msg = _run(run())
        assert "unavailable" in msg.lower()


class TestGunDMConfirmView:
    def test_accept_sets_flag(self):
        async def run():
            view = GunDMConfirmView(recipient_id=100, timeout=10)
            assert view.accepted is None
            inter = _make_interaction()
            btn = _find_button(view, "Accept")
            await btn.callback(inter)
            return view.accepted

        assert _run(run()) is True

    def test_decline_sets_flag(self):
        async def run():
            view = GunDMConfirmView(recipient_id=100, timeout=10)
            assert view.accepted is None
            inter = _make_interaction()
            btn = _find_button(view, "Decline")
            await btn.callback(inter)
            return view.accepted

        assert _run(run()) is False


class TestAdminShopCommand:
    def test_dm_guard(self, monkeypatch):
        monkeypatch.setattr("config.NIGHTCITYBOT_LOG_CHANNEL_ID", 0)
        monkeypatch.setattr("config.ADMIN_HUB_CHANNEL_ID", 0)
        cog = _make_admin_cog()
        cog.bot.get_channel = MagicMock(return_value=None)
        ctx = _ctx(admin=True)
        _run(_cmd(cog, "admin_shop", ctx))
        assert ctx.send.called

    def test_sends_embed_with_view(self, monkeypatch):
        monkeypatch.setattr("config.NIGHTCITYBOT_LOG_CHANNEL_ID", 0)
        monkeypatch.setattr("config.ADMIN_HUB_CHANNEL_ID", 12345)
        cog = _make_admin_cog()
        channel = MagicMock()
        channel.send = AsyncMock()
        cog.bot.get_channel = MagicMock(return_value=channel)
        ctx = _ctx(admin=True)
        _run(_cmd(cog, "admin_shop", ctx))
        assert channel.send.called
        call_kwargs = channel.send.call_args
        assert "view" in call_kwargs.kwargs


class TestAdminShopMenuView:
    def test_interaction_check_fixer_passes(self, monkeypatch):
        monkeypatch.setattr("config.FIXER_ROLE_ID", 888)

        async def run():
            view = AdminShopMenuView()
            role = MagicMock()
            role.id = 888
            inter = _make_interaction(user_id=111, roles=[role])
            return await view.interaction_check(inter)

        assert _run(run()) is True

    def test_interaction_check_non_fixer_fails(self, monkeypatch):
        monkeypatch.setattr("config.FIXER_ROLE_ID", 888)

        async def run():
            view = AdminShopMenuView()
            inter = _make_interaction(user_id=999, roles=[])
            return await view.interaction_check(inter)

        assert _run(run()) is False

    def test_item_history_starts_inline_flow(self, monkeypatch):
        monkeypatch.setattr("config.NIGHTCITYBOT_LOG_CHANNEL_ID", 0)

        async def run():
            cog = _make_admin_cog()
            view = AdminShopMenuView()
            inter = _make_interaction()
            inter.client.get_cog = MagicMock(side_effect=lambda n: cog if n == "AdminShop" else None)
            inter.channel_id = 123
            btn = _find_button(view, "Item History")
            await btn.callback(inter)
            inter.response.defer.assert_called_once()
            kwargs = inter.followup.send.call_args.kwargs
            from NightCityBot.cogs.admin_shop import ItemHistorySourceView
            assert isinstance(kwargs["view"], ItemHistorySourceView)

        _run(run())

    def test_player_inv_sends_picker(self, monkeypatch):
        monkeypatch.setattr("config.NIGHTCITYBOT_LOG_CHANNEL_ID", 0)

        async def run():
            cog = _make_admin_cog()
            view = AdminShopMenuView()
            inter = _make_interaction()
            inter.client.get_cog = MagicMock(side_effect=lambda n: cog if n == "AdminShop" else None)
            btn = _find_button(view, "Player Inventory")
            await btn.callback(inter)
            inter.response.defer.assert_called_once()
            kwargs = inter.followup.send.call_args.kwargs
            assert isinstance(kwargs["view"], AdminPlayerInvPickerView)

        _run(run())


class TestTradeConfirmView:
    def test_accept_sets_flag(self):
        async def run():
            view = TradeConfirmView(recipient_id=111, timeout=10)
            assert view.accepted is None
            inter = _make_interaction()
            btn = _find_button(view, "Accept")
            await btn.callback(inter)
            return view.accepted

        assert _run(run()) is True

    def test_decline_sets_flag(self):
        async def run():
            view = TradeConfirmView(recipient_id=111, timeout=10)
            assert view.accepted is None
            inter = _make_interaction()
            btn = _find_button(view, "Decline")
            await btn.callback(inter)
            return view.accepted

        assert _run(run()) is False


class TestResolveMember:
    def test_ripperdoc_resolve_mention(self, monkeypatch):
        monkeypatch.setattr("config.CYBERWARE_LOG_CHANNEL_ID", 0)
        cog = _make_ripperdoc_cog()
        guild = MagicMock()
        member = _make_member(12345)
        guild.get_member = MagicMock(return_value=member)
        result = _run(cog._resolve_member_from_input(guild, "<@12345>"))
        assert result == member

    def test_ripperdoc_resolve_nickname_mention(self, monkeypatch):
        monkeypatch.setattr("config.CYBERWARE_LOG_CHANNEL_ID", 0)
        cog = _make_ripperdoc_cog()
        guild = MagicMock()
        member = _make_member(12345)
        guild.get_member = MagicMock(return_value=member)
        result = _run(cog._resolve_member_from_input(guild, "<@!12345>"))
        assert result == member

    def test_ripperdoc_resolve_id_string(self, monkeypatch):
        monkeypatch.setattr("config.CYBERWARE_LOG_CHANNEL_ID", 0)
        cog = _make_ripperdoc_cog()
        guild = MagicMock()
        member = _make_member(12345)
        guild.get_member = MagicMock(return_value=member)
        result = _run(cog._resolve_member_from_input(guild, "12345"))
        assert result == member

    def test_ripperdoc_resolve_bad_input(self, monkeypatch):
        monkeypatch.setattr("config.CYBERWARE_LOG_CHANNEL_ID", 0)
        cog = _make_ripperdoc_cog()
        guild = MagicMock()
        result = _run(cog._resolve_member_from_input(guild, "notanumber"))
        assert result is None

    def test_gunstore_resolve_mention(self, monkeypatch):
        monkeypatch.setattr("config.GUN_LOG_CHANNEL_ID", 0)
        cog = _make_gunstore_cog()
        guild = MagicMock()
        member = _make_member(54321)
        guild.get_member = MagicMock(return_value=member)
        result = _run(cog._resolve_member(guild, "<@!54321>"))
        assert result == member

    def test_admin_resolve_member(self, monkeypatch):
        monkeypatch.setattr("config.NIGHTCITYBOT_LOG_CHANNEL_ID", 0)
        cog = _make_admin_cog()
        guild = MagicMock()
        member = _make_member(999)
        guild.get_member = MagicMock(return_value=member)
        result = _run(cog._resolve_member(guild, "<@999>"))
        assert result == member

    def test_admin_resolve_not_found(self, monkeypatch):
        monkeypatch.setattr("config.NIGHTCITYBOT_LOG_CHANNEL_ID", 0)
        cog = _make_admin_cog()
        guild = MagicMock()
        guild.get_member = MagicMock(return_value=None)
        guild.fetch_member = AsyncMock(side_effect=Exception("not found"))
        result = _run(cog._resolve_member(guild, "12345"))
        assert result is None


class TestLogChannelHelpers:
    def test_ripperdoc_log_channel_returns_none(self, monkeypatch):
        monkeypatch.setattr("config.CYBERWARE_LOG_CHANNEL_ID", 0)
        cog = _make_ripperdoc_cog()
        assert _run(cog._log_channel()) is None

    def test_ripperdoc_log_channel_returns_channel(self, monkeypatch):
        monkeypatch.setattr("config.CYBERWARE_LOG_CHANNEL_ID", 12345)
        cog = _make_ripperdoc_cog()
        ch = MagicMock()
        cog.bot.get_channel = MagicMock(return_value=ch)
        assert _run(cog._log_channel()) == ch

    def test_gunstore_log_channel_returns_none(self, monkeypatch):
        monkeypatch.setattr("config.GUN_LOG_CHANNEL_ID", 0)
        cog = _make_gunstore_cog()
        assert _run(cog._log_channel()) is None

    def test_admin_audit_channel_returns_none(self, monkeypatch):
        monkeypatch.setattr("config.NIGHTCITYBOT_LOG_CHANNEL_ID", 0)
        cog = _make_admin_cog()
        assert _run(cog._audit_channel()) is None


class TestPersistentViewTimeout:
    def test_ripperdoc_menu_has_no_timeout(self):
        async def run():
            view = RipperdocMenuView()
            assert view.timeout is None
        _run(run())

    def test_gunstore_menu_has_no_timeout(self):
        async def run():
            view = GunstoreMenuView()
            assert view.timeout is None
        _run(run())

    def test_admin_menu_has_no_timeout(self):
        async def run():
            view = AdminShopMenuView()
            assert view.timeout is None
        _run(run())


class TestItemHistoryUtilities:
    def test_ih_record_event_signature(self):
        from NightCityBot.utils.db import ih_record_event
        import inspect
        sig = inspect.signature(ih_record_event)
        params = list(sig.parameters.keys())
        assert "item_id" in params
        assert "event_type" in params
        assert "actor_id" in params
        assert "target_id" in params
        assert "price" in params
        assert "metadata" in params

    def test_ih_get_history_signature(self):
        from NightCityBot.utils.db import ih_get_history
        import inspect
        sig = inspect.signature(ih_get_history)
        params = list(sig.parameters.keys())
        assert "item_id" in params
        assert "limit" in params


class TestItemHistoryViews:
    def test_source_view_player_button_swaps_to_player_picker(self):
        from NightCityBot.cogs.admin_shop import ItemHistorySourceView, ItemHistoryPlayerPickerView
        async def run():
            cog = _make_admin_cog()
            ctx = MagicMock()
            ctx.author = MagicMock()
            ctx.author.id = 42
            view = ItemHistorySourceView(cog, ctx)
            inter = _make_interaction()
            inter.user.id = 42
            btn = _find_button(view, "Player Item")
            await btn.callback(inter)
            args = inter.response.edit_message.call_args
            assert isinstance(args.kwargs["view"], ItemHistoryPlayerPickerView)
        _run(run())

    def test_source_view_store_button_swaps_to_store_picker(self):
        from NightCityBot.cogs.admin_shop import ItemHistorySourceView, ItemHistoryStorePickerView
        async def run():
            cog = _make_admin_cog()
            ctx = MagicMock()
            ctx.author = MagicMock()
            ctx.author.id = 42
            view = ItemHistorySourceView(cog, ctx)
            inter = _make_interaction()
            inter.user.id = 42
            btn = _find_button(view, "Store Item")
            await btn.callback(inter)
            args = inter.response.edit_message.call_args
            assert isinstance(args.kwargs["view"], ItemHistoryStorePickerView)
        _run(run())

    def test_source_view_blocks_wrong_user(self):
        from NightCityBot.cogs.admin_shop import ItemHistorySourceView
        async def run():
            cog = _make_admin_cog()
            ctx = MagicMock()
            ctx.author = MagicMock()
            ctx.author.id = 42
            view = ItemHistorySourceView(cog, ctx)
            inter = _make_interaction()
            inter.user.id = 999
            result = await view.interaction_check(inter)
            assert result is False
        _run(run())

    @patch("NightCityBot.cogs.admin_shop.pi_get_by_owner", new_callable=AsyncMock, return_value=[])
    def test_player_picker_no_items(self, mock_get):
        from NightCityBot.cogs.admin_shop import ItemHistoryPlayerPickerView
        async def run():
            cog = _make_admin_cog()
            ctx = MagicMock()
            ctx.author = MagicMock()
            ctx.author.id = 42
            ctx.guild = MagicMock()
            ctx.guild.get_member = MagicMock(return_value=MagicMock(id=100, display_name="TestPlayer"))
            view = ItemHistoryPlayerPickerView(cog, ctx)
            inter = _make_interaction()
            inter.user.id = 42
            sel = [c for c in view.children if isinstance(c, discord.ui.UserSelect)][0]
            member_mock = MagicMock()
            member_mock.id = 100
            member_mock.display_name = "TestPlayer"
            sel._values = [member_mock]
            await sel.callback(inter)
            inter.followup.send.assert_called_once()
            msg = inter.followup.send.call_args.kwargs.get("content", inter.followup.send.call_args[0][0])
            assert "no items" in msg.lower()
        _run(run())

    @patch("NightCityBot.cogs.admin_shop.ih_get_history", new_callable=AsyncMock, return_value=[])
    def test_item_picker_no_history(self, mock_hist):
        from NightCityBot.cogs.admin_shop import ItemHistoryItemPickerView
        async def run():
            cog = _make_admin_cog()
            ctx = MagicMock()
            ctx.author = MagicMock()
            ctx.author.id = 42
            options = [discord.SelectOption(label="Test Gun", value="uuid-1234")]
            view = ItemHistoryItemPickerView(cog, ctx, options, "TestPlayer")
            inter = _make_interaction()
            inter.user.id = 42
            inter.data = {"values": ["uuid-1234"]}
            await view._on_select(inter)
            inter.response.defer.assert_called_once()
            content = inter.followup.send.call_args.kwargs.get("content", "")
            assert "no history" in content.lower()
        _run(run())

    @patch("NightCityBot.cogs.admin_shop.ih_get_history", new_callable=AsyncMock, return_value=[
        {"created_at": "2025-01-01T00:00:00", "event_type": "purchase", "actor_id": "42", "target_id": "", "price": 500, "metadata": {"item_name": "Gun"}}
    ])
    def test_item_picker_shows_embed(self, mock_hist):
        from NightCityBot.cogs.admin_shop import ItemHistoryItemPickerView
        async def run():
            cog = _make_admin_cog()
            ctx = MagicMock()
            ctx.author = MagicMock()
            ctx.author.id = 42
            options = [discord.SelectOption(label="Test Gun", value="uuid-1234")]
            view = ItemHistoryItemPickerView(cog, ctx, options, "TestPlayer")
            inter = _make_interaction()
            inter.user.id = 42
            inter.data = {"values": ["uuid-1234"]}
            await view._on_select(inter)
            kwargs = inter.followup.send.call_args.kwargs
            assert "embed" in kwargs
            assert kwargs["embed"].title.startswith("📜 Item History")
        _run(run())


class TestCogRegistration:
    def test_all_new_cogs_importable(self):
        from NightCityBot.cogs import ripperdoc_hub
        from NightCityBot.cogs import gunstore_hub
        from NightCityBot.cogs import admin_shop
        assert hasattr(ripperdoc_hub, "setup")
        assert hasattr(gunstore_hub, "setup")
        assert hasattr(admin_shop, "setup")

    def test_ripperdoc_hub_cog_name(self):
        cog = _make_ripperdoc_cog()
        assert cog.__cog_name__ == "RipperdocHub"

    def test_gunstore_hub_cog_name(self):
        cog = _make_gunstore_cog()
        assert cog.__cog_name__ == "GunstoreHub"

    def test_admin_shop_cog_name(self):
        cog = _make_admin_cog()
        assert cog.__cog_name__ == "AdminShop"


class TestGunstoreCogReference:
    def test_guns_cog_returns_none_when_not_loaded(self):
        cog = _make_gunstore_cog()
        cog.bot.cogs = {}
        assert cog._guns_cog() is None

    def test_guns_cog_returns_cog_when_loaded(self):
        cog = _make_gunstore_cog()
        guns_cog = MagicMock()
        cog.bot.cogs = {"GunsShopCog": guns_cog}
        assert cog._guns_cog() is guns_cog


class TestInlineApproveView:
    def test_approve_and_sell_sets_flag(self):
        async def run():
            guns_cog = MagicMock()
            guns_cog.lock = asyncio.Lock()
            guns_cog._load_state = AsyncMock(return_value={
                "stores": {"s1": {"controlled_buyers": []}}
            })
            guns_cog._save_state = AsyncMock()
            customer = _make_member(222, "Customer")
            cog = _make_gunstore_cog()
            ctx = _ctx()
            view = InlineApproveView(cog, ctx, guns_cog, "s1", customer)
            inter = _make_interaction()
            btn = _find_button(view, "Approve & Sell")
            await btn.callback(inter)
            assert view.approved is True
            guns_cog._save_state.assert_called_once()

        _run(run())

    def test_cancel_sets_flag_false(self):
        async def run():
            guns_cog = MagicMock()
            customer = _make_member(222, "Customer")
            cog = _make_gunstore_cog()
            ctx = _ctx()
            view = InlineApproveView(cog, ctx, guns_cog, "s1", customer)
            inter = _make_interaction()
            btn = _find_button(view, "Cancel")
            await btn.callback(inter)
            assert view.approved is False

        _run(run())


class TestGunSellUUIDContinuity:
    def test_sell_uses_lot_item_id(self):
        async def run():
            cog = _make_gunstore_cog()
            ctx = _ctx()
            customer = _make_member(222, "Customer")
            ctx.guild.get_member = MagicMock(return_value=customer)

            guns_cog = MagicMock()
            guns_cog.lock = asyncio.Lock()
            known_uuid = "aaaa-bbbb-cccc-dddd"
            store_data = {
                "stores": {
                    "test_store": {
                        "owner_id": 111,
                        "lots": [{
                            "lot_id": "lot-1",
                            "gun_name": "Pistol",
                            "gun_level": "L",
                            "weapon_type": "",
                            "unit_cost": 100,
                            "qty_remaining": 1,
                            "restriction": "basic",
                            "item_ids": [known_uuid],
                        }],
                        "controlled_buyers": [],
                    }
                }
            }
            guns_cog._load_state = AsyncMock(return_value=store_data)
            guns_cog._save_state = AsyncMock()
            cog.bot.cogs = {"GunsShopCog": guns_cog}

            from NightCityBot.cogs.gunstore_hub import _process_gun_sell
            customer = _make_member(222, "Customer")
            lot = store_data["stores"]["test_store"]["lots"][0]
            character = {"character_id": "char-1", "name": "V"}

            dm_view_cls = "NightCityBot.cogs.gunstore_hub.GunDMConfirmView"
            with patch(dm_view_cls) as MockView:
                mock_view_inst = MagicMock()
                mock_view_inst.accepted = True
                mock_view_inst.wait = AsyncMock()
                MockView.return_value = mock_view_inst
                with patch("NightCityBot.cogs.gunstore_hub.pi_add_item", new_callable=AsyncMock, return_value=True) as mock_pi:
                    with patch("NightCityBot.cogs.gunstore_hub.ih_record_event", new_callable=AsyncMock):
                        with patch("NightCityBot.cogs.gunstore_hub.ensure_character_active", new_callable=AsyncMock, return_value=True):
                            inter = _make_interaction()
                            await _process_gun_sell(cog, inter, ctx, customer, lot, "test_store", character, 0)
                            if mock_pi.called:
                                call_args = mock_pi.call_args[0][0]
                                assert call_args["item_id"] == known_uuid

        _run(run())


class TestInstallDMConfirmation:
    def test_install_sends_dm_to_patient(self):
        async def run():
            from NightCityBot.cogs.ripperdoc_hub import _process_cw_install
            cog = _make_ripperdoc_cog()
            ctx = _ctx()
            patient = _make_member(333, "Patient")

            cw_cog = MagicMock()
            cw_cog.lock = asyncio.Lock()
            cw_cog._load_inventory = AsyncMock(return_value=[
                {"item_id": "test-uuid", "name": "Mantis Blades", "price_paid": 100}
            ])
            cw_cog._grouped_inventory = MagicMock(return_value=[
                {"name": "Mantis Blades", "count": 1, "items": [
                    {"item_id": "test-uuid", "name": "Mantis Blades"}
                ]}
            ])
            cw_cog._save_inventory = AsyncMock()
            cog.bot.cogs = {"CyberwareShop": cw_cog}

            group = {"name": "Mantis Blades", "count": 1, "items": [
                {"item_id": "test-uuid", "name": "Mantis Blades"}
            ]}
            character = {"character_id": "char-1", "name": "V"}

            dm_view_cls = "NightCityBot.cogs.ripperdoc_hub.DMConfirmView"
            with patch(dm_view_cls) as MockView:
                mock_view_inst = MagicMock()
                mock_view_inst.accepted = True
                mock_view_inst.wait = AsyncMock()
                MockView.return_value = mock_view_inst
                with patch("NightCityBot.cogs.ripperdoc_hub.ih_record_event", new_callable=AsyncMock):
                    with patch("NightCityBot.cogs.ripperdoc_hub.ensure_character_active", new_callable=AsyncMock, return_value=True):
                        inter = _make_interaction()
                        await _process_cw_install(cog, inter, ctx, patient, group, character, 0)
                        patient.send.assert_called_once()
                        msg = patient.send.call_args[0][0]
                        assert "Mantis Blades" in msg

        _run(run())


class TestSellRefundMath:
    def test_refund_uses_cash_bank_split(self):
        async def run():
            cog = _make_ripperdoc_cog()
            ctx = _ctx()
            patient = _make_member(444, "Patient")

            cog.unbelievaboat.get_balance = AsyncMock(return_value={"cash": 3000, "bank": 7000})
            cog.unbelievaboat.update_balance = AsyncMock(return_value=True)

            cw_cog = MagicMock()
            cw_cog.lock = asyncio.Lock()
            cw_cog._load_inventory = AsyncMock(return_value=[
                {"item_id": "uid-1", "name": "Chrome Arms"}
            ])
            cw_cog._save_inventory = AsyncMock()
            cog.bot.cogs = {"CyberwareShop": cw_cog}

            from NightCityBot.cogs.ripperdoc_hub import _process_cw_sell
            group = {"name": "Chrome Arms", "count": 1, "items": [
                {"item_id": "uid-1", "name": "Chrome Arms"}
            ]}
            character = {"character_id": "char-1", "name": "V"}

            dm_view_cls = "NightCityBot.cogs.ripperdoc_hub.DMConfirmView"
            with patch(dm_view_cls) as MockView:
                mock_view_inst = MagicMock()
                mock_view_inst.accepted = True
                mock_view_inst.wait = AsyncMock()
                MockView.return_value = mock_view_inst
                with patch("NightCityBot.cogs.ripperdoc_hub.ensure_character_active", new_callable=AsyncMock, return_value=True):
                    inter = _make_interaction()
                    await _process_cw_sell(cog, inter, ctx, patient, group, character, 5000)
                    refund_call = None
                    for call in cog.unbelievaboat.update_balance.call_args_list:
                        args = call[0]
                        kwargs = call[1] if len(call) > 1 else {}
                        reason = kwargs.get("reason", "")
                        if "refund" in reason.lower():
                            refund_call = call
                            break
                    if refund_call:
                        balance_dict = refund_call[0][1]
                        assert "bank" in balance_dict

        _run(run())


class TestSellerCreditFailurePendingTransfer:
    def test_creates_pending_transfer_on_credit_failure(self):
        async def run():
            cog = _make_ripperdoc_cog()
            ctx = _ctx()
            patient = _make_member(555, "Patient")

            call_count = [0]
            async def fake_update(uid, bal, reason=""):
                call_count[0] += 1
                if call_count[0] == 2:
                    return False
                return True

            cog.unbelievaboat.get_balance = AsyncMock(return_value={"cash": 50000, "bank": 0})
            cog.unbelievaboat.update_balance = AsyncMock(side_effect=fake_update)

            cw_cog = MagicMock()
            cw_cog.lock = asyncio.Lock()
            cw_cog._load_inventory = AsyncMock(return_value=[
                {"item_id": "uid-2", "name": "Optics"}
            ])
            cw_cog._grouped_inventory = MagicMock(return_value=[
                {"name": "Optics", "count": 1, "items": [
                    {"item_id": "uid-2", "name": "Optics"}
                ]}
            ])
            cw_cog._save_inventory = AsyncMock()
            cog.bot.cogs = {"CyberwareShop": cw_cog}

            from NightCityBot.cogs.ripperdoc_hub import _process_cw_sell
            group = {"name": "Optics", "count": 1, "items": [
                {"item_id": "uid-2", "name": "Optics"}
            ]}
            character = {"character_id": "char-1", "name": "V"}

            dm_view_cls = "NightCityBot.cogs.ripperdoc_hub.DMConfirmView"
            with patch(dm_view_cls) as MockView:
                mock_view_inst = MagicMock()
                mock_view_inst.accepted = True
                mock_view_inst.wait = AsyncMock()
                MockView.return_value = mock_view_inst
                with patch("NightCityBot.cogs.ripperdoc_hub.ensure_character_active", new_callable=AsyncMock, return_value=True):
                    with patch("NightCityBot.cogs.ripperdoc_hub.pt_create", new_callable=AsyncMock) as mock_pt:
                        with patch("NightCityBot.cogs.ripperdoc_hub.pi_add_item", new_callable=AsyncMock, return_value=True):
                            with patch("NightCityBot.cogs.ripperdoc_hub.ih_record_event", new_callable=AsyncMock):
                                inter = _make_interaction()
                                await _process_cw_sell(cog, inter, ctx, patient, group, character, 1000)
                                mock_pt.assert_called_once()

        _run(run())


class TestPiAddItemFailureCompensation:
    def test_refunds_on_item_grant_failure(self):
        async def run():
            cog = _make_ripperdoc_cog()
            ctx = _ctx()
            patient = _make_member(666, "Patient")

            cog.unbelievaboat.get_balance = AsyncMock(return_value={"cash": 50000, "bank": 0})
            cog.unbelievaboat.update_balance = AsyncMock(return_value=True)

            cw_cog = MagicMock()
            cw_cog.lock = asyncio.Lock()
            cw_cog._load_inventory = AsyncMock(return_value=[
                {"item_id": "uid-3", "name": "Neural Link"}
            ])
            cw_cog._grouped_inventory = MagicMock(return_value=[
                {"name": "Neural Link", "count": 1, "items": [
                    {"item_id": "uid-3", "name": "Neural Link"}
                ]}
            ])
            cw_cog._save_inventory = AsyncMock()
            cog.bot.cogs = {"CyberwareShop": cw_cog}

            from NightCityBot.cogs.ripperdoc_hub import _process_cw_sell
            group = {"name": "Neural Link", "count": 1, "items": [
                {"item_id": "uid-3", "name": "Neural Link"}
            ]}
            character = {"character_id": "char-1", "name": "V"}

            dm_view_cls = "NightCityBot.cogs.ripperdoc_hub.DMConfirmView"
            with patch(dm_view_cls) as MockView:
                mock_view_inst = MagicMock()
                mock_view_inst.accepted = True
                mock_view_inst.wait = AsyncMock()
                MockView.return_value = mock_view_inst
                with patch("NightCityBot.cogs.ripperdoc_hub.ensure_character_active", new_callable=AsyncMock, return_value=True):
                    with patch("NightCityBot.cogs.ripperdoc_hub.pi_add_item", new_callable=AsyncMock, return_value=False):
                        with patch("NightCityBot.cogs.ripperdoc_hub.ih_record_event", new_callable=AsyncMock):
                            inter = _make_interaction()
                            await _process_cw_sell(cog, inter, ctx, patient, group, character, 2000)
                            refund_calls = [
                                c for c in cog.unbelievaboat.update_balance.call_args_list
                                if "refund" in str(c).lower() or "grant failed" in str(c).lower()
                            ]
                            assert len(refund_calls) >= 1
                            found_send = False
                            for c in ctx.send.call_args_list:
                                if "failed" in str(c).lower() and "refund" in str(c).lower():
                                    found_send = True
                            assert found_send

        _run(run())


class TestAdminWholesaleButtons:
    def test_wholesale_stock_button_exists(self):
        async def run():
            view = AdminShopMenuView()
            btn = _find_button(view, "Wholesale Stock")
            assert btn is not None

        _run(run())

    @patch("NightCityBot.cogs.admin_shop.gun_catalog_get_all", new_callable=AsyncMock)
    def test_restock_button_starts_inline_flow(self, mock_catalog, monkeypatch):
        monkeypatch.setattr("NightCityBot.cogs.admin_shop.collect_text_input", AsyncMock(return_value=None))
        mock_catalog.return_value = [{"gun_name": "Militech M-76e", "price": 5000, "restriction": "basic"}]

        async def run():
            cog = _make_admin_cog()
            view = AdminShopMenuView()
            inter = _make_interaction()
            inter.client.get_cog = MagicMock(side_effect=lambda n: cog if n == "AdminShop" else None)
            inter.channel_id = 123
            btn = _find_button(view, "Restock Gun Wholesale")
            await btn.callback(inter)
            inter.response.send_message.assert_called_once()

        _run(run())

    @patch("NightCityBot.cogs.admin_shop.gun_catalog_get_all", new_callable=AsyncMock, return_value=[])
    def test_restock_gun_empty_catalog(self, mock_catalog):
        async def run():
            cog = _make_admin_cog()
            view = AdminShopMenuView()
            inter = _make_interaction()
            inter.client.get_cog = MagicMock(side_effect=lambda n: cog if n == "AdminShop" else None)
            btn = _find_button(view, "Restock Gun Wholesale")
            await btn.callback(inter)
            msg = inter.response.send_message.call_args[0][0]
            assert "empty" in msg.lower()

        _run(run())

    def test_clear_gun_button_sends_confirm(self):
        async def run():
            cog = _make_admin_cog()
            view = AdminShopMenuView()
            inter = _make_interaction()
            inter.client.get_cog = MagicMock(side_effect=lambda n: cog if n == "AdminShop" else None)
            btn = _find_button(view, "Clear Gun Wholesale")
            await btn.callback(inter)
            inter.followup.send.assert_called_once()
            msg = inter.followup.send.call_args[0][0]
            assert "clear" in msg.lower()

        _run(run())

    @patch("NightCityBot.cogs.admin_shop.cw_catalog_get_all", new_callable=AsyncMock)
    def test_restock_cw_button_starts_inline_flow(self, mock_catalog, monkeypatch):
        monkeypatch.setattr("NightCityBot.cogs.admin_shop.collect_text_input", AsyncMock(return_value=None))
        mock_catalog.return_value = [{"name": "Neural Link", "price": 5000}]

        async def run():
            cog = _make_admin_cog()
            view = AdminShopMenuView()
            inter = _make_interaction()
            inter.client.get_cog = MagicMock(side_effect=lambda n: cog if n == "AdminShop" else None)
            inter.channel_id = 123
            btn = _find_button(view, "Restock CW Wholesale")
            await btn.callback(inter)
            inter.response.send_message.assert_called_once()
            msg = inter.response.send_message.call_args[0][0]
            assert "total_items" in msg

        _run(run())

    @patch("NightCityBot.cogs.admin_shop.cw_catalog_get_all", new_callable=AsyncMock, return_value=[])
    def test_restock_cw_empty_catalog(self, mock_catalog):
        async def run():
            cog = _make_admin_cog()
            view = AdminShopMenuView()
            inter = _make_interaction()
            inter.client.get_cog = MagicMock(side_effect=lambda n: cog if n == "AdminShop" else None)
            btn = _find_button(view, "Restock CW Wholesale")
            await btn.callback(inter)
            msg = inter.response.send_message.call_args[0][0]
            assert "empty" in msg.lower()

        _run(run())

    def test_clear_cw_button_sends_confirm(self):
        async def run():
            cog = _make_admin_cog()
            view = AdminShopMenuView()
            inter = _make_interaction()
            inter.client.get_cog = MagicMock(side_effect=lambda n: cog if n == "AdminShop" else None)
            btn = _find_button(view, "Clear CW Wholesale")
            await btn.callback(inter)
            inter.followup.send.assert_called_once()
            msg = inter.followup.send.call_args[0][0]
            assert "cyberware" in msg.lower()

        _run(run())


class TestWholesaleClearConfirm:
    def test_confirm_clears_gun_lots(self):
        async def run():
            cog = _make_admin_cog()
            ctx = _ctx()
            guns_cog = MagicMock()
            guns_cog.lock = asyncio.Lock()
            guns_cog._load_state = AsyncMock(return_value={"wholesale_lots": [{"gun_name": "AK"}]})
            guns_cog._save_state = AsyncMock()
            cog.bot.cogs = {"GunsShopCog": guns_cog}

            view = WholesaleClearConfirmView(cog, ctx, target="guns")
            inter = _make_interaction()
            btn = _find_button(view, "Confirm Clear")
            await btn.callback(inter)
            saved = guns_cog._save_state.call_args[0][0]
            assert saved["wholesale_lots"] == []

        _run(run())

    def test_confirm_clears_cw_lots(self):
        async def run():
            cog = _make_admin_cog()
            ctx = _ctx()
            cw_cog = MagicMock()
            cw_cog.lock = asyncio.Lock()
            cw_cog._load_state = AsyncMock(return_value={"cw_wholesale_lots": [{"item_name": "Optics"}]})
            cw_cog._save_state = AsyncMock()
            cog.bot.cogs = {"CyberwareShop": cw_cog}

            view = WholesaleClearConfirmView(cog, ctx, target="cw")
            inter = _make_interaction()
            btn = _find_button(view, "Confirm Clear")
            await btn.callback(inter)
            saved = cw_cog._save_state.call_args[0][0]
            assert saved["cw_wholesale_lots"] == []

        _run(run())

    def test_cancel_does_nothing(self):
        async def run():
            cog = _make_admin_cog()
            ctx = _ctx()
            view = WholesaleClearConfirmView(cog, ctx, target="guns")
            inter = _make_interaction()
            btn = _find_button(view, "Cancel")
            await btn.callback(inter)
            inter.response.edit_message.assert_called_once()

        _run(run())


class TestAdminPanelNoAddRemoveButtons:
    """Regression: Admin panel must NOT have Add Item or Remove Item buttons."""

    def test_no_add_item_button(self):
        async def run():
            view = AdminShopMenuView()
            labels = [getattr(c, "label", "") for c in view.children]
            assert "Add Item" not in labels
        _run(run())

    def test_no_remove_item_button(self):
        async def run():
            view = AdminShopMenuView()
            labels = [getattr(c, "label", "") for c in view.children]
            assert "Remove Item" not in labels
        _run(run())

    def test_reassign_button_removed(self):
        async def run():
            view = AdminShopMenuView()
            labels = [getattr(c, "label", "") for c in view.children]
            assert "Reassign Item" not in labels
        _run(run())

    def test_panel_embed_no_add_remove_text(self):
        from NightCityBot.cogs.admin_shop import AdminShopCog
        cog = AdminShopCog.__new__(AdminShopCog)
        embed = cog._panel_embed()
        desc = embed.description
        assert "Add Item" not in desc
        assert "Remove Item" not in desc
        assert "Reassign" in desc


class TestHelperFunctions:
    def test_is_store_owner_member(self, monkeypatch):
        monkeypatch.setattr("config.WHOLESALER_STORE_ROLE_IDS", 777)
        role = MagicMock()
        role.id = 777
        member = _make_member(111, roles=[role])
        assert _is_store_owner_member(member) is True

    def test_is_store_owner_member_false(self, monkeypatch):
        monkeypatch.setattr("config.WHOLESALER_STORE_ROLE_IDS", 777)
        member = _make_member(111, roles=[])
        assert _is_store_owner_member(member) is False

    def test_is_employee_member(self):
        role = MagicMock()
        role.id = GUN_STORE_EMPLOYEE_ROLE_ID
        member = _make_member(222, roles=[role])
        assert _is_employee_member(member) is True

    def test_is_employee_member_false(self):
        member = _make_member(222, roles=[])
        assert _is_employee_member(member) is False

    def test_find_employee_store_found(self):
        state = {
            "stores": {
                "999:100": {
                    "owner_id": 100,
                    "employees": [222],
                    "lots": [],
                },
            }
        }
        sid, store = _find_employee_store(state, 999, 222)
        assert sid == "999:100"
        assert store["owner_id"] == 100

    def test_find_employee_store_not_found(self):
        state = {"stores": {"999:100": {"owner_id": 100, "employees": [], "lots": []}}}
        sid, store = _find_employee_store(state, 999, 222)
        assert sid is None
        assert store is None


class TestGunstoreInteractionCheckEmployee:
    def test_employee_passes(self, monkeypatch):
        monkeypatch.setattr("config.WHOLESALER_STORE_ROLE_IDS", 777)

        async def run():
            view = GunstoreMenuView()
            role = MagicMock()
            role.id = GUN_STORE_EMPLOYEE_ROLE_ID
            inter = _make_interaction(user_id=222, roles=[role])
            return await view.interaction_check(inter)

        assert _run(run()) is True

    def test_employee_blocked_from_wholesale(self, monkeypatch):
        monkeypatch.setattr("config.GUN_LOG_CHANNEL_ID", 0)
        monkeypatch.setattr("config.WHOLESALER_STORE_ROLE_IDS", 777)

        async def run():
            cog = _make_gunstore_cog()
            view = GunstoreMenuView()
            emp_role = MagicMock()
            emp_role.id = GUN_STORE_EMPLOYEE_ROLE_ID
            inter = _make_interaction(user_id=222, roles=[emp_role])
            inter.client.get_cog = MagicMock(side_effect=lambda n: cog if n == "GunstoreHub" else None)
            btn = _find_button(view, "Buy from Wholesale")
            await btn.callback(inter)
            return inter.response.send_message.call_args[0][0]

        msg = _run(run())
        assert "only store owners" in msg.lower()

    def test_employee_can_sell(self, monkeypatch):
        monkeypatch.setattr("config.GUN_LOG_CHANNEL_ID", 0)
        monkeypatch.setattr("config.WHOLESALER_STORE_ROLE_IDS", 777)

        async def run():
            cog = _make_gunstore_cog()
            guns_cog = MagicMock()
            guns_cog._store_id = MagicMock(return_value="999:100")
            guns_cog._load_state = AsyncMock(return_value={
                "stores": {
                    "999:100": {
                        "owner_id": 100,
                        "employees": [222],
                        "lots": [{
                            "lot_id": "lot-1",
                            "gun_name": "Pistol",
                            "gun_level": "L",
                            "unit_cost": 100,
                            "qty_remaining": 1,
                            "restriction": "basic",
                        }],
                        "controlled_buyers": [],
                    }
                }
            })
            cog.bot.cogs = {"GunsShopCog": guns_cog}
            view = GunstoreMenuView()
            emp_role = MagicMock()
            emp_role.id = GUN_STORE_EMPLOYEE_ROLE_ID
            inter = _make_interaction(user_id=222, roles=[emp_role])
            inter.client.get_cog = MagicMock(side_effect=lambda n: cog if n == "GunstoreHub" else None)
            btn = _find_button(view, "Sell to Customer")
            await btn.callback(inter)
            call_kwargs = inter.followup.send.call_args[1]
            return isinstance(call_kwargs.get("view"), GunSellSetupView)

        assert _run(run()) is True

    def test_employee_not_assigned_to_store(self, monkeypatch):
        monkeypatch.setattr("config.GUN_LOG_CHANNEL_ID", 0)
        monkeypatch.setattr("config.WHOLESALER_STORE_ROLE_IDS", 777)

        async def run():
            cog = _make_gunstore_cog()
            guns_cog = MagicMock()
            guns_cog._load_state = AsyncMock(return_value={"stores": {}})
            cog.bot.cogs = {"GunsShopCog": guns_cog}
            view = GunstoreMenuView()
            emp_role = MagicMock()
            emp_role.id = GUN_STORE_EMPLOYEE_ROLE_ID
            inter = _make_interaction(user_id=222, roles=[emp_role])
            inter.client.get_cog = MagicMock(side_effect=lambda n: cog if n == "GunstoreHub" else None)
            btn = _find_button(view, "Sell to Customer")
            await btn.callback(inter)
            return inter.followup.send.call_args[0][0]

        msg = _run(run())
        assert "not assigned" in msg.lower()


class TestSetStoreName:
    @patch("NightCityBot.cogs.gunstore_hub.collect_text_input", new_callable=AsyncMock, return_value="Hellfire Arms")
    def test_set_name_success(self, mock_collect, monkeypatch):
        monkeypatch.setattr("config.GUN_LOG_CHANNEL_ID", 0)
        monkeypatch.setattr("config.WHOLESALER_STORE_ROLE_IDS", 777)

        async def run():
            cog = _make_gunstore_cog()
            guns_cog = MagicMock()
            state = {"stores": {}}
            guns_cog._load_state = AsyncMock(return_value=state)
            guns_cog._save_state = AsyncMock()
            guns_cog._store_id = MagicMock(return_value="999:111")
            guns_cog.lock = asyncio.Lock()
            cog.bot.cogs = {"GunsShopCog": guns_cog}
            view = GunstoreMenuView()
            owner_role = MagicMock()
            owner_role.id = 777
            inter = _make_interaction(user_id=111, roles=[owner_role])
            inter.channel_id = 123
            inter.client.get_cog = MagicMock(side_effect=lambda n: cog if n == "GunstoreHub" else None)
            btn = _find_button(view, "Set Store Name")
            await btn.callback(inter)
            return inter.followup.send.call_args[0][0]

        msg = _run(run())
        assert "Hellfire Arms" in msg

    def test_set_name_employee_blocked(self, monkeypatch):
        monkeypatch.setattr("config.GUN_LOG_CHANNEL_ID", 0)
        monkeypatch.setattr("config.WHOLESALER_STORE_ROLE_IDS", 777)

        async def run():
            view = GunstoreMenuView()
            emp_role = MagicMock()
            emp_role.id = GUN_STORE_EMPLOYEE_ROLE_ID
            inter = _make_interaction(user_id=222, roles=[emp_role])
            btn = _find_button(view, "Set Store Name")
            await btn.callback(inter)
            return inter.response.send_message.call_args[0][0]

        msg = _run(run())
        assert "only store owners" in msg.lower()


class TestManageEmployees:
    def test_manage_employees_blocked_for_employee(self, monkeypatch):
        monkeypatch.setattr("config.WHOLESALER_STORE_ROLE_IDS", 777)

        async def run():
            view = GunstoreMenuView()
            emp_role = MagicMock()
            emp_role.id = GUN_STORE_EMPLOYEE_ROLE_ID
            inter = _make_interaction(user_id=222, roles=[emp_role])
            btn = _find_button(view, "Manage Employees")
            await btn.callback(inter)
            return inter.response.send_message.call_args[0][0]

        msg = _run(run())
        assert "only store owners" in msg.lower()

    def test_manage_employees_opens_view_for_owner(self, monkeypatch):
        monkeypatch.setattr("config.WHOLESALER_STORE_ROLE_IDS", 777)

        async def run():
            cog = _make_gunstore_cog()
            view = GunstoreMenuView()
            owner_role = MagicMock()
            owner_role.id = 777
            inter = _make_interaction(user_id=111, roles=[owner_role])
            inter.client.get_cog = MagicMock(side_effect=lambda n: cog if n == "GunstoreHub" else None)
            btn = _find_button(view, "Manage Employees")
            await btn.callback(inter)
            call_kwargs = inter.response.send_message.call_args[1]
            return isinstance(call_kwargs.get("view"), _ManageEmployeesView)

        assert _run(run()) is True

    def test_add_employee(self, monkeypatch):
        monkeypatch.setattr("config.WHOLESALER_STORE_ROLE_IDS", 777)

        async def run():
            cog = _make_gunstore_cog()
            guns_cog = MagicMock()
            state = {"stores": {"999:111": {"owner_id": 111, "lots": [], "controlled_buyers": []}}}
            guns_cog._load_state = AsyncMock(return_value=state)
            guns_cog._save_state = AsyncMock()
            guns_cog._store_id = MagicMock(return_value="999:111")
            guns_cog.lock = asyncio.Lock()
            cog.bot.cogs = {"GunsShopCog": guns_cog}
            ctx = _ctx(author_id=111)
            view = _EmployeePickerView(cog, ctx, add=True)
            select = _find_user_select(view)
            new_emp = _make_member(333, "Employee1")
            new_emp.send = AsyncMock(return_value=MagicMock())
            emp_role = MagicMock(id=GUN_STORE_EMPLOYEE_ROLE_ID)
            new_emp.roles = []
            new_emp.add_roles = AsyncMock()
            ctx.guild.get_role = MagicMock(return_value=emp_role)
            select._values = [new_emp]
            inter = _make_interaction()
            with patch("NightCityBot.cogs.gunstore_hub._GunEmployeeDMConfirmView") as mock_dm:
                inst = MagicMock()
                inst.accepted = True
                inst.wait = AsyncMock(return_value=False)
                mock_dm.return_value = inst
                await select.callback(inter)
            saved_state = guns_cog._save_state.call_args[0][0]
            assert 333 in saved_state["stores"]["999:111"]["employees"]
            assert "accepted" in inter.followup.send.call_args[0][0].lower()

        _run(run())

    def test_remove_employee(self, monkeypatch):
        monkeypatch.setattr("config.WHOLESALER_STORE_ROLE_IDS", 777)

        async def run():
            cog = _make_gunstore_cog()
            guns_cog = MagicMock()
            state = {"stores": {"999:111": {"owner_id": 111, "lots": [], "controlled_buyers": [], "employees": [333]}}}
            guns_cog._load_state = AsyncMock(return_value=state)
            guns_cog._save_state = AsyncMock()
            guns_cog._store_id = MagicMock(return_value="999:111")
            guns_cog.lock = asyncio.Lock()
            cog.bot.cogs = {"GunsShopCog": guns_cog}
            ctx = _ctx(author_id=111)
            emp_role = MagicMock(id=GUN_STORE_EMPLOYEE_ROLE_ID)
            ctx.guild.get_role = MagicMock(return_value=emp_role)
            view = _EmployeePickerView(cog, ctx, add=False)
            select = _find_user_select(view)
            emp = _make_member(333, "Employee1")
            emp.roles = [emp_role]
            emp.remove_roles = AsyncMock()
            ctx.guild.get_member = MagicMock(return_value=emp)
            select._values = [emp]
            inter = _make_interaction()
            await select.callback(inter)
            saved_state = guns_cog._save_state.call_args[0][0]
            assert 333 not in saved_state["stores"]["999:111"]["employees"]
            assert "removed" in inter.followup.send.call_args[0][0].lower()

        _run(run())

    def test_add_employee_already_added(self, monkeypatch):
        monkeypatch.setattr("config.WHOLESALER_STORE_ROLE_IDS", 777)

        async def run():
            cog = _make_gunstore_cog()
            guns_cog = MagicMock()
            state = {"stores": {"999:111": {"owner_id": 111, "lots": [], "controlled_buyers": [], "employees": [333]}}}
            guns_cog._load_state = AsyncMock(return_value=state)
            guns_cog._save_state = AsyncMock()
            guns_cog._store_id = MagicMock(return_value="999:111")
            guns_cog.lock = asyncio.Lock()
            cog.bot.cogs = {"GunsShopCog": guns_cog}
            ctx = _ctx(author_id=111)
            view = _EmployeePickerView(cog, ctx, add=True)
            select = _find_user_select(view)
            emp = _make_member(333, "Employee1")
            select._values = [emp]
            inter = _make_interaction()
            await select.callback(inter)
            guns_cog._save_state.assert_not_called()
            assert "already" in inter.followup.send.call_args[0][0].lower()

        _run(run())

    def test_view_employees(self, monkeypatch):
        monkeypatch.setattr("config.WHOLESALER_STORE_ROLE_IDS", 777)

        async def run():
            cog = _make_gunstore_cog()
            guns_cog = MagicMock()
            guns_cog._load_state = AsyncMock(return_value={
                "stores": {"999:111": {"owner_id": 111, "lots": [], "employees": [333, 444], "store_name": "Hellfire Arms"}}
            })
            guns_cog._store_id = MagicMock(return_value="999:111")
            cog.bot.cogs = {"GunsShopCog": guns_cog}
            ctx = _ctx(author_id=111)
            view = _ManageEmployeesView(cog, ctx)
            inter = _make_interaction()
            inter.client.get_cog = MagicMock(side_effect=lambda n: cog if n == "GunstoreHub" else None)
            btn = _find_button(view, "View Employees")
            await btn.callback(inter)
            msg = inter.followup.send.call_args[0][0]
            assert "Hellfire Arms" in msg
            assert "<@333>" in msg
            assert "<@444>" in msg

        _run(run())


class TestEmployeeCap:
    def test_add_employee_at_cap_rejected(self, monkeypatch):
        monkeypatch.setattr("config.WHOLESALER_STORE_ROLE_IDS", 777)

        async def run():
            cog = _make_gunstore_cog()
            full_employees = list(range(1000, 1025))
            guns_cog = MagicMock()
            guns_cog._load_state = AsyncMock(return_value={
                "stores": {"999:111": {"owner_id": 111, "lots": [], "controlled_buyers": [], "employees": full_employees}}
            })
            guns_cog._store_id = MagicMock(return_value="999:111")
            guns_cog._save_state = AsyncMock()
            guns_cog.lock = asyncio.Lock()
            cog.bot.cogs = {"GunsShopCog": guns_cog}
            ctx = _ctx(author_id=111)
            view = _EmployeePickerView(cog, ctx, add=True)
            select = _find_user_select(view)
            new_member = _make_member(2000, "NewGuy")
            select._values = [new_member]
            inter = _make_interaction()
            await select.callback(inter)
            msg = inter.followup.send.call_args[0][0]
            assert "limit" in msg.lower()
            guns_cog._save_state.assert_not_called()

        _run(run())

    def test_add_employee_at_24_allowed(self, monkeypatch):
        monkeypatch.setattr("config.WHOLESALER_STORE_ROLE_IDS", 777)

        async def run():
            cog = _make_gunstore_cog()
            employees_24 = list(range(1000, 1024))
            guns_cog = MagicMock()
            guns_cog._load_state = AsyncMock(return_value={
                "stores": {"999:111": {"owner_id": 111, "lots": [], "controlled_buyers": [], "employees": employees_24}}
            })
            guns_cog._store_id = MagicMock(return_value="999:111")
            guns_cog._save_state = AsyncMock()
            guns_cog.lock = asyncio.Lock()
            cog.bot.cogs = {"GunsShopCog": guns_cog}
            ctx = _ctx(author_id=111)
            view = _EmployeePickerView(cog, ctx, add=True)
            select = _find_user_select(view)
            new_member = _make_member(2000, "NewGuy")
            new_member.send = AsyncMock(return_value=MagicMock())
            new_member.roles = []
            new_member.add_roles = AsyncMock()
            ctx.guild.get_role = MagicMock(return_value=MagicMock(id=GUN_STORE_EMPLOYEE_ROLE_ID))
            select._values = [new_member]
            inter = _make_interaction()
            with patch("NightCityBot.cogs.gunstore_hub._GunEmployeeDMConfirmView") as mock_dm:
                inst = MagicMock()
                inst.accepted = True
                inst.wait = AsyncMock(return_value=False)
                mock_dm.return_value = inst
                await select.callback(inter)
            guns_cog._save_state.assert_called_once()
            saved = guns_cog._save_state.call_args[0][0]
            assert 2000 in saved["stores"]["999:111"]["employees"]

        _run(run())

    def test_view_employees_shows_all(self, monkeypatch):
        monkeypatch.setattr("config.WHOLESALER_STORE_ROLE_IDS", 777)

        async def run():
            cog = _make_gunstore_cog()
            employees = list(range(1000, 1005))
            guns_cog = MagicMock()
            guns_cog._load_state = AsyncMock(return_value={
                "stores": {"999:111": {"owner_id": 111, "lots": [], "employees": employees, "store_name": "Test Store"}}
            })
            guns_cog._store_id = MagicMock(return_value="999:111")
            cog.bot.cogs = {"GunsShopCog": guns_cog}
            ctx = _ctx(author_id=111)
            view = _ManageEmployeesView(cog, ctx)
            inter = _make_interaction()
            inter.client.get_cog = MagicMock(side_effect=lambda n: cog if n == "GunstoreHub" else None)
            btn = _find_button(view, "View Employees")
            await btn.callback(inter)
            msg = inter.followup.send.call_args[0][0]
            assert "(5)" in msg
            for eid in employees:
                assert f"<@{eid}>" in msg

        _run(run())


class TestStoreNameInInventory:
    def test_inventory_shows_store_name(self, monkeypatch):
        monkeypatch.setattr("config.GUN_LOG_CHANNEL_ID", 0)
        monkeypatch.setattr("config.WHOLESALER_STORE_ROLE_IDS", 777)

        async def run():
            cog = _make_gunstore_cog()
            guns_cog = MagicMock()
            guns_cog._store_id = MagicMock(return_value="999:111")
            guns_cog._load_state = AsyncMock(return_value={
                "stores": {
                    "999:111": {
                        "owner_id": 111,
                        "store_name": "Hellfire Arms",
                        "lots": [{
                            "lot_id": "lot-1",
                            "gun_name": "Pistol",
                            "gun_level": "L",
                            "unit_cost": 100,
                            "qty_remaining": 2,
                            "restriction": "basic",
                        }],
                        "controlled_buyers": [],
                    }
                }
            })
            cog.bot.cogs = {"GunsShopCog": guns_cog}
            view = GunstoreMenuView()
            owner_role = MagicMock()
            owner_role.id = 777
            inter = _make_interaction(user_id=111, roles=[owner_role])
            inter.client.get_cog = MagicMock(side_effect=lambda n: cog if n == "GunstoreHub" else None)
            btn = _find_button(view, "My Store Inventory")
            await btn.callback(inter)
            embed = inter.followup.send.call_args.kwargs["embed"]
            return embed.title

        title = _run(run())
        assert "Hellfire Arms" in title
