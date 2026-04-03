"""Tests for the !fixer interactive hub."""

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from NightCityBot.cogs.fixer_hub import (
    FixerHubCog,
    FixerTopView,
    PlayerSubView,
    StoreSubView,
    WholesalerSubView,
    PlayerInvPickerView,
    PlayerAddItemPickerView,
    PlayerRemoveItemView,
    RemoveItemPickerView,
    LOAPickerView,
    StoreOwnerPickerView,
    StoreActionView,
    StoreRemoveLotPickerView,
    GUN_STORE_OWNER_ROLE_ID,
    RIPPERDOC_ROLE_ID,
)


def _make_bot():
    bot = MagicMock()
    bot.get_channel = MagicMock(return_value=None)
    bot.fetch_channel = AsyncMock(side_effect=Exception("no channel"))
    bot.cogs = {}
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


def _make_interaction(user_id=111, cog=None, roles=None, admin=False):
    inter = MagicMock(spec=discord.Interaction)
    inter.user = MagicMock(spec=discord.Member)
    inter.user.id = user_id
    inter.user.display_name = f"User{user_id}"
    inter.user.mention = f"<@{user_id}>"
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
    inter.message = MagicMock()
    inter.message.delete = AsyncMock()
    inter.guild = MagicMock()
    inter.guild.id = 999
    inter.guild.get_member = MagicMock(return_value=None)
    inter.guild.fetch_member = AsyncMock(return_value=None)
    inter.client = MagicMock()
    inter.client.get_cog = MagicMock(side_effect=lambda n: cog if n == "FixerHub" else None)
    inter.client.get_guild = MagicMock(return_value=inter.guild)
    return inter


def _make_member(member_id, name="Member", roles=None):
    m = MagicMock(spec=discord.Member)
    m.id = member_id
    m.display_name = name
    m.mention = f"<@{member_id}>"
    m.roles = roles or []
    m.add_roles = AsyncMock()
    m.remove_roles = AsyncMock()
    m.guild_permissions = MagicMock()
    m.guild_permissions.administrator = False
    return m


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


def _make_cog():
    bot = _make_bot()
    cog = FixerHubCog.__new__(FixerHubCog)
    cog.bot = bot
    return cog


def _run(coro):
    return asyncio.run(coro)


class TestFixerCommand:
    def test_channel_not_found(self):
        cog = _make_cog()
        cog.bot.get_channel = MagicMock(return_value=None)
        ctx = _ctx(admin=True)
        _run(cog.fixer.callback(cog, ctx))
        ctx.send.assert_called_once()
        assert "not found" in ctx.send.call_args[0][0].lower()

    def test_posts_panel_to_channel(self):
        cog = _make_cog()
        channel = MagicMock()
        channel.send = AsyncMock()
        cog.bot.get_channel = MagicMock(return_value=channel)
        ctx = _ctx(admin=True)
        ctx.message = MagicMock()
        ctx.message.delete = AsyncMock()
        _run(cog.fixer.callback(cog, ctx))
        channel.send.assert_called_once()
        kwargs = channel.send.call_args.kwargs
        assert isinstance(kwargs["view"], FixerTopView)


class TestTopViewNavigation:
    def test_player_button_sends_ephemeral(self):
        async def _test():
            cog = _make_cog()
            view = FixerTopView()
            inter = _make_interaction(cog=cog, admin=True)
            btn = _find_button(view, "Player")
            await btn.callback(inter)
            inter.response.send_message.assert_called_once()
            kwargs = inter.response.send_message.call_args.kwargs
            assert isinstance(kwargs["view"], PlayerSubView)
        _run(_test())

    def test_store_button_sends_ephemeral(self):
        async def _test():
            cog = _make_cog()
            view = FixerTopView()
            inter = _make_interaction(cog=cog, admin=True)
            btn = _find_button(view, "Store")
            await btn.callback(inter)
            inter.response.send_message.assert_called_once()
            kwargs = inter.response.send_message.call_args.kwargs
            assert isinstance(kwargs["view"], StoreSubView)
        _run(_test())

    def test_wholesaler_button_sends_ephemeral(self):
        async def _test():
            cog = _make_cog()
            view = FixerTopView()
            inter = _make_interaction(cog=cog, admin=True)
            btn = _find_button(view, "Wholesaler")
            await btn.callback(inter)
            inter.response.send_message.assert_called_once()
            kwargs = inter.response.send_message.call_args.kwargs
            assert isinstance(kwargs["view"], WholesalerSubView)
        _run(_test())

    def test_interaction_check_admin(self):
        async def _test():
            view = FixerTopView()
            inter = _make_interaction(user_id=111, admin=True)
            inter.guild.get_member = MagicMock(return_value=inter.user)
            assert await view.interaction_check(inter)
        _run(_test())

    def test_interaction_check_no_role(self):
        async def _test():
            view = FixerTopView()
            inter = _make_interaction(user_id=222, admin=False)
            inter.guild.get_member = MagicMock(return_value=inter.user)
            assert not await view.interaction_check(inter)
        _run(_test())


class TestPlayerSubViewButtons:
    def test_view_inventory_sends_picker(self):
        async def _test():
            cog = _make_cog()
            ctx = _ctx()
            view = PlayerSubView(cog, ctx)
            inter = _make_interaction()
            btn = _find_button(view, "View Inventory")
            await btn.callback(inter)
            inter.followup.send.assert_called_once()
            kwargs = inter.followup.send.call_args.kwargs
            assert isinstance(kwargs["view"], PlayerInvPickerView)
        _run(_test())

    def test_add_item_sends_picker(self):
        async def _test():
            cog = _make_cog()
            ctx = _ctx()
            view = PlayerSubView(cog, ctx)
            inter = _make_interaction()
            btn = _find_button(view, "Add Item")
            await btn.callback(inter)
            kwargs = inter.followup.send.call_args.kwargs
            assert isinstance(kwargs["view"], PlayerAddItemPickerView)
        _run(_test())

    def test_remove_item_starts_inline_flow(self):
        async def _test():
            cog = _make_cog()
            ctx = _ctx()
            view = PlayerSubView(cog, ctx)
            inter = _make_interaction()
            btn = _find_button(view, "Remove Item")
            await btn.callback(inter)
            inter.response.defer.assert_called_once()
            kwargs = inter.followup.send.call_args.kwargs
            assert "view" in kwargs
        _run(_test())

    @patch("NightCityBot.cogs.fixer_hub.collect_text_input", new_callable=AsyncMock, return_value=None)
    def test_reassign_starts_inline_flow(self, mock_collect):
        async def _test():
            cog = _make_cog()
            ctx = _ctx()
            view = PlayerSubView(cog, ctx)
            inter = _make_interaction()
            inter.channel_id = 123
            btn = _find_button(view, "Reassign Item")
            await btn.callback(inter)
            inter.response.defer.assert_called_once()
        _run(_test())

    def test_start_loa_sends_picker(self):
        async def _test():
            cog = _make_cog()
            ctx = _ctx()
            view = PlayerSubView(cog, ctx)
            inter = _make_interaction()
            btn = _find_button(view, "Start LOA")
            await btn.callback(inter)
            kwargs = inter.followup.send.call_args.kwargs
            picker = kwargs["view"]
            assert isinstance(picker, LOAPickerView)
            assert picker.action == "start"
        _run(_test())

    def test_end_loa_sends_picker(self):
        async def _test():
            cog = _make_cog()
            ctx = _ctx()
            view = PlayerSubView(cog, ctx)
            inter = _make_interaction()
            btn = _find_button(view, "End LOA")
            await btn.callback(inter)
            kwargs = inter.followup.send.call_args.kwargs
            picker = kwargs["view"]
            assert isinstance(kwargs["view"], LOAPickerView)
            assert picker.action == "end"
        _run(_test())

class TestStoreSubViewButtons:
    def test_view_gun_store_with_role_members(self):
        async def _test():
            cog = _make_cog()
            ctx = _ctx()
            member1 = _make_member(200, "GunGuy")
            role = MagicMock()
            role.members = [member1]
            ctx.guild.get_role = MagicMock(side_effect=lambda rid: role if rid == GUN_STORE_OWNER_ROLE_ID else None)
            view = StoreSubView(cog, ctx)
            inter = _make_interaction()
            btn = _find_button(view, "View Gun Store")
            await btn.callback(inter)
            kwargs = inter.followup.send.call_args.kwargs
            picker = kwargs["view"]
            assert isinstance(picker, StoreOwnerPickerView)
            assert picker.store_type == "gun"
        _run(_test())

    def test_view_gun_store_no_owners(self):
        async def _test():
            cog = _make_cog()
            ctx = _ctx()
            role = MagicMock()
            role.members = []
            ctx.guild.get_role = MagicMock(return_value=role)
            view = StoreSubView(cog, ctx)
            inter = _make_interaction()
            btn = _find_button(view, "View Gun Store")
            await btn.callback(inter)
            msg = inter.followup.send.call_args[0][0]
            assert "no gun store owners" in msg.lower()
        _run(_test())

    def test_view_ripperdoc_store_with_role_members(self):
        async def _test():
            cog = _make_cog()
            ctx = _ctx()
            member1 = _make_member(300, "DocRipper")
            role = MagicMock()
            role.members = [member1]
            ctx.guild.get_role = MagicMock(side_effect=lambda rid: role if rid == RIPPERDOC_ROLE_ID else None)
            view = StoreSubView(cog, ctx)
            inter = _make_interaction()
            btn = _find_button(view, "View Ripperdoc Store")
            await btn.callback(inter)
            kwargs = inter.followup.send.call_args.kwargs
            picker = kwargs["view"]
            assert isinstance(picker, StoreOwnerPickerView)
            assert picker.store_type == "cw"
        _run(_test())

    def test_view_ripperdoc_store_no_docs(self):
        async def _test():
            cog = _make_cog()
            ctx = _ctx()
            role = MagicMock()
            role.members = []
            ctx.guild.get_role = MagicMock(return_value=role)
            view = StoreSubView(cog, ctx)
            inter = _make_interaction()
            btn = _find_button(view, "View Ripperdoc Store")
            await btn.callback(inter)
            msg = inter.followup.send.call_args[0][0]
            assert "no ripperdocs" in msg.lower()
        _run(_test())

class TestWholesalerSubViewButtons:
    def test_view_stock_empty(self):
        async def _test():
            cog = _make_cog()
            ctx = _ctx()
            view = WholesalerSubView(cog, ctx)
            inter = _make_interaction()
            btn = _find_button(view, "View Stock")
            await btn.callback(inter)
            inter.followup.send.assert_called_once()
            assert "No wholesale" in inter.followup.send.call_args[0][0]
        _run(_test())

    def test_view_stock_with_guns(self):
        async def _test():
            cog = _make_cog()
            guns_cog = MagicMock()
            guns_cog._load_state = AsyncMock(return_value={
                "wholesale_lots": [
                    {"gun_name": "TestGun", "unit_cost": 1000, "qty_available": 5, "restriction": "basic"}
                ]
            })
            cog.bot.cogs["GunsShopCog"] = guns_cog
            ctx = _ctx()
            view = WholesalerSubView(cog, ctx)
            inter = _make_interaction()
            btn = _find_button(view, "View Stock")
            await btn.callback(inter)
            inter.followup.send.assert_called_once()
            embed = inter.followup.send.call_args.kwargs["embed"]
            assert "TestGun" in embed.description
        _run(_test())

    @patch("NightCityBot.cogs.fixer_hub.collect_text_input", new_callable=AsyncMock, return_value=None)
    def test_add_gun_starts_inline_flow(self, mock_collect):
        async def _test():
            cog = _make_cog()
            ctx = _ctx()
            view = WholesalerSubView(cog, ctx)
            inter = _make_interaction()
            inter.channel_id = 123
            btn = _find_button(view, "Add Gun")
            await btn.callback(inter)
            inter.response.defer.assert_called_once()
        _run(_test())

    @patch("NightCityBot.cogs.fixer_hub.collect_text_input", new_callable=AsyncMock, return_value=None)
    def test_add_cw_starts_inline_flow(self, mock_collect):
        async def _test():
            cog = _make_cog()
            ctx = _ctx()
            view = WholesalerSubView(cog, ctx)
            inter = _make_interaction()
            inter.channel_id = 123
            btn = _find_button(view, "Add Cyberware")
            await btn.callback(inter)
            inter.response.defer.assert_called_once()
        _run(_test())

    def test_remove_gun_empty(self):
        async def _test():
            cog = _make_cog()
            guns_cog = MagicMock()
            guns_cog._load_state = AsyncMock(return_value={"wholesale_lots": []})
            cog.bot.cogs["GunsShopCog"] = guns_cog
            ctx = _ctx()
            view = WholesalerSubView(cog, ctx)
            inter = _make_interaction()
            btn = _find_button(view, "Remove Gun")
            await btn.callback(inter)
            msg = inter.followup.send.call_args[0][0] if inter.followup.send.call_args[0] else inter.followup.send.call_args.kwargs.get("content", "")
            assert "empty" in msg.lower()
        _run(_test())

    def test_remove_gun_shows_picker(self):
        async def _test():
            cog = _make_cog()
            guns_cog = MagicMock()
            guns_cog._load_state = AsyncMock(return_value={
                "wholesale_lots": [
                    {"lot_id": "lot-1", "gun_name": "TestGun", "unit_cost": 1000, "qty_available": 5, "restriction": "basic"}
                ]
            })
            cog.bot.cogs["GunsShopCog"] = guns_cog
            ctx = _ctx()
            view = WholesalerSubView(cog, ctx)
            inter = _make_interaction()
            btn = _find_button(view, "Remove Gun")
            await btn.callback(inter)
            kwargs = inter.followup.send.call_args.kwargs
            from NightCityBot.cogs.fixer_hub import WHRemoveGunPickerView
            assert isinstance(kwargs["view"], WHRemoveGunPickerView)
        _run(_test())

    def test_remove_cw_empty(self):
        async def _test():
            cog = _make_cog()
            cw_cog = MagicMock()
            cw_cog._load_state = AsyncMock(return_value={"cw_wholesale_lots": []})
            cog.bot.cogs["CyberwareShop"] = cw_cog
            ctx = _ctx()
            view = WholesalerSubView(cog, ctx)
            inter = _make_interaction()
            btn = _find_button(view, "Remove Cyberware")
            await btn.callback(inter)
            msg = inter.followup.send.call_args[0][0] if inter.followup.send.call_args[0] else inter.followup.send.call_args.kwargs.get("content", "")
            assert "empty" in msg.lower()
        _run(_test())

    def test_remove_cw_shows_picker(self):
        async def _test():
            cog = _make_cog()
            cw_cog = MagicMock()
            cw_cog._load_state = AsyncMock(return_value={
                "cw_wholesale_lots": [
                    {"lot_id": "cwlot-1", "item_name": "Neural Link", "unit_cost": 5000, "qty_available": 3}
                ]
            })
            cog.bot.cogs["CyberwareShop"] = cw_cog
            ctx = _ctx()
            view = WholesalerSubView(cog, ctx)
            inter = _make_interaction()
            btn = _find_button(view, "Remove Cyberware")
            await btn.callback(inter)
            kwargs = inter.followup.send.call_args.kwargs
            from NightCityBot.cogs.fixer_hub import WHRemoveCWPickerView
            assert isinstance(kwargs["view"], WHRemoveCWPickerView)
        _run(_test())


class TestPlayerPickerViews:
    @patch("NightCityBot.cogs.fixer_hub.pi_get_by_owner", new_callable=AsyncMock)
    def test_inv_picker_success(self, mock_get):
        async def _test():
            cog = _make_cog()
            ctx = _ctx()
            member = _make_member(222, "TestPlayer")
            mock_get.return_value = [
                {"item_type": "gun", "name": "Pistol", "character_name": "V", "item_id": "abc12345"},
            ]
            view = PlayerInvPickerView(cog, ctx)
            select = _find_user_select(view)
            select._values = [member]
            inter = _make_interaction()
            await select.callback(inter)
            embed = inter.followup.send.call_args.kwargs["embed"]
            assert "Pistol" in embed.description
        _run(_test())

    @patch("NightCityBot.cogs.fixer_hub.pi_get_by_owner", new_callable=AsyncMock, return_value=[])
    def test_inv_picker_empty(self, mock_get):
        async def _test():
            cog = _make_cog()
            ctx = _ctx()
            member = _make_member(222, "TestPlayer")
            view = PlayerInvPickerView(cog, ctx)
            select = _find_user_select(view)
            select._values = [member]
            inter = _make_interaction()
            await select.callback(inter)
            assert "no items" in inter.followup.send.call_args[0][0].lower()
        _run(_test())

    def test_add_item_picker_continue_no_player(self):
        async def _test():
            cog = _make_cog()
            ctx = _ctx()
            view = PlayerAddItemPickerView(cog, ctx)
            inter = _make_interaction()
            btn = _find_button(view, "Continue →")
            await btn.callback(inter)
            assert "select a player" in inter.response.send_message.call_args[0][0].lower()
        _run(_test())

    @patch("NightCityBot.cogs.fixer_hub.collect_text_input", new_callable=AsyncMock, return_value=None)
    @patch("NightCityBot.cogs.fixer_hub.ensure_character_active", new_callable=AsyncMock, return_value=True)
    def test_add_item_picker_continue_starts_inline_flow(self, mock_active, mock_collect):
        async def _test():
            cog = _make_cog()
            ctx = _ctx()
            view = PlayerAddItemPickerView(cog, ctx)
            view.selected_player = _make_member(222, "TestPlayer")
            view.selected_character = {"character_id": "char-1", "name": "V"}
            inter = _make_interaction()
            inter.channel_id = 123
            btn = _find_button(view, "Continue →")
            await btn.callback(inter)
            inter.response.defer.assert_called_once()
        _run(_test())

    @patch("NightCityBot.cogs.fixer_hub._audit_channel", new_callable=AsyncMock, return_value=None)
    @patch("NightCityBot.cogs.fixer_hub.ih_record_event", new_callable=AsyncMock)
    @patch("NightCityBot.cogs.fixer_hub.pi_add_item", new_callable=AsyncMock, return_value=True)
    @patch("NightCityBot.cogs.fixer_hub.ensure_character_active", new_callable=AsyncMock, return_value=True)
    def test_add_item_details_success(self, mock_active, mock_add, mock_record, mock_audit):
        async def _test():
            from NightCityBot.cogs.fixer_hub import _process_fixer_add_item
            cog = _make_cog()
            member = _make_member(222, "TestPlayer")
            inter = _make_interaction()
            await _process_fixer_add_item(
                cog, inter, member,
                {"character_id": "char-1", "name": "V"},
                "Katana, gun, 2, 3000",
            )
            assert mock_add.call_count == 2
            assert "Katana" in inter.followup.send.call_args[0][0]
        _run(_test())



class TestLOAPickerView:
    def test_start_loa(self):
        async def _test():
            cog = _make_cog()
            ctx = _ctx()
            loa_role = MagicMock()
            loa_role.id = 9999
            loa_cog = MagicMock()
            loa_cog.get_loa_role = MagicMock(return_value=loa_role)
            cog.bot.get_cog = MagicMock(return_value=loa_cog)
            member = _make_member(222, "TestPlayer", roles=[])
            view = LOAPickerView(cog, ctx, action="start")
            select = _find_user_select(view)
            select._values = [member]
            inter = _make_interaction()
            await select.callback(inter)
            member.add_roles.assert_called_once()
            assert "now on LOA" in inter.followup.send.call_args[0][0]
        _run(_test())

    def test_start_loa_already(self):
        async def _test():
            cog = _make_cog()
            ctx = _ctx()
            loa_role = MagicMock()
            loa_role.id = 9999
            loa_cog = MagicMock()
            loa_cog.get_loa_role = MagicMock(return_value=loa_role)
            cog.bot.get_cog = MagicMock(return_value=loa_cog)
            role_with_id = MagicMock()
            role_with_id.id = 9999
            member = _make_member(222, "TestPlayer", roles=[role_with_id])
            view = LOAPickerView(cog, ctx, action="start")
            select = _find_user_select(view)
            select._values = [member]
            inter = _make_interaction()
            await select.callback(inter)
            member.add_roles.assert_not_called()
            assert "already on LOA" in inter.followup.send.call_args[0][0]
        _run(_test())

    def test_end_loa(self):
        async def _test():
            cog = _make_cog()
            ctx = _ctx()
            loa_role = MagicMock()
            loa_role.id = 9999
            loa_cog = MagicMock()
            loa_cog.get_loa_role = MagicMock(return_value=loa_role)
            cog.bot.get_cog = MagicMock(return_value=loa_cog)
            role_with_id = MagicMock()
            role_with_id.id = 9999
            member = _make_member(222, "TestPlayer", roles=[role_with_id])
            view = LOAPickerView(cog, ctx, action="end")
            select = _find_user_select(view)
            select._values = [member]
            inter = _make_interaction()
            await select.callback(inter)
            member.remove_roles.assert_called_once()
            assert "LOA has ended" in inter.followup.send.call_args[0][0]
        _run(_test())

    def test_end_loa_not_on(self):
        async def _test():
            cog = _make_cog()
            ctx = _ctx()
            loa_role = MagicMock()
            loa_role.id = 9999
            loa_cog = MagicMock()
            loa_cog.get_loa_role = MagicMock(return_value=loa_role)
            cog.bot.get_cog = MagicMock(return_value=loa_cog)
            member = _make_member(222, "TestPlayer", roles=[])
            view = LOAPickerView(cog, ctx, action="end")
            select = _find_user_select(view)
            select._values = [member]
            inter = _make_interaction()
            await select.callback(inter)
            member.remove_roles.assert_not_called()
            assert "not currently on LOA" in inter.followup.send.call_args[0][0]
        _run(_test())


class TestStorePickerViews:
    def test_owner_picker_gun_store_empty(self):
        async def _test():
            cog = _make_cog()
            ctx = _ctx()
            owner = _make_member(222, "ShopOwner")
            ctx.guild.get_member = MagicMock(return_value=owner)
            guns_cog = MagicMock()
            guns_cog._load_state = AsyncMock(return_value={"stores": {}})
            guns_cog._store_id = MagicMock(return_value="999:222")
            cog.bot.cogs["GunsShopCog"] = guns_cog
            options = [discord.SelectOption(label="ShopOwner", value="222")]
            view = StoreOwnerPickerView(cog, ctx, options, store_type="gun")
            inter = _make_interaction()
            inter.data = {"values": ["222"]}
            await view._on_select(inter)
            embed = inter.followup.send.call_args.kwargs.get("embed")
            assert embed is not None
            assert "empty" in embed.description.lower()
        _run(_test())

    def test_owner_picker_gun_store_with_stock(self):
        async def _test():
            cog = _make_cog()
            ctx = _ctx()
            owner = _make_member(222, "ShopOwner")
            ctx.guild.get_member = MagicMock(return_value=owner)
            guns_cog = MagicMock()
            guns_cog._load_state = AsyncMock(return_value={
                "stores": {"999:222": {"lots": [
                    {"gun_name": "Rifle", "unit_cost": 2000, "qty_remaining": 3, "restriction": "basic"},
                ]}}
            })
            guns_cog._store_id = MagicMock(return_value="999:222")
            cog.bot.cogs["GunsShopCog"] = guns_cog
            options = [discord.SelectOption(label="ShopOwner", value="222")]
            view = StoreOwnerPickerView(cog, ctx, options, store_type="gun")
            inter = _make_interaction()
            inter.data = {"values": ["222"]}
            await view._on_select(inter)
            embed = inter.followup.send.call_args.kwargs["embed"]
            assert "Rifle" in embed.description
            action_view = inter.followup.send.call_args.kwargs["view"]
            assert isinstance(action_view, StoreActionView)
        _run(_test())

    def test_owner_picker_cw_store(self):
        async def _test():
            cog = _make_cog()
            ctx = _ctx()
            owner = _make_member(222, "Doc")
            ctx.guild.get_member = MagicMock(return_value=owner)
            cw_cog = MagicMock()
            cw_cog._load_state = AsyncMock(return_value={"ripperdoc_stores": {}})
            cw_cog._load_inventory = AsyncMock(return_value=[
                {"name": "Sandevistan", "price_paid": 5000, "purchased_at": "2025-01-01"},
            ])
            cw_cog._grouped_inventory = MagicMock(return_value=[
                {"name": "Sandevistan", "price_paid": 5000, "count": 1, "date": "2025-01-01"},
            ])
            cog.bot.cogs["CyberwareShop"] = cw_cog
            options = [discord.SelectOption(label="Doc", value="222")]
            view = StoreOwnerPickerView(cog, ctx, options, store_type="cw")
            inter = _make_interaction()
            inter.data = {"values": ["222"]}
            await view._on_select(inter)
            embed = inter.followup.send.call_args.kwargs["embed"]
            assert "Sandevistan" in embed.description
        _run(_test())

    @patch("NightCityBot.cogs.fixer_hub._audit_channel", new_callable=AsyncMock, return_value=None)
    def test_store_add_gun_success(self, mock_audit):
        async def _test():
            from NightCityBot.cogs.fixer_hub import _process_store_add_gun
            cog = _make_cog()
            owner = _make_member(222, "ShopOwner")
            guns_cog = MagicMock()
            state = {"stores": {}}
            guns_cog._load_state = AsyncMock(return_value=state)
            guns_cog._save_state = AsyncMock(return_value=True)
            guns_cog._store_id = MagicMock(return_value="999:222")
            guns_cog.lock = asyncio.Lock()
            cog.bot.cogs["GunsShopCog"] = guns_cog
            inter = _make_interaction()
            await _process_store_add_gun(cog, inter, owner, "TestGun, 5, 1000, basic")
            guns_cog._save_state.assert_called_once()
            assert "TestGun" in inter.followup.send.call_args[0][0]
        _run(_test())

    @patch("NightCityBot.cogs.fixer_hub._audit_channel", new_callable=AsyncMock, return_value=None)
    def test_store_add_cw_success(self, mock_audit):
        async def _test():
            from NightCityBot.cogs.fixer_hub import _process_store_add_cw
            cog = _make_cog()
            owner = _make_member(222, "Doc")
            cw_cog = MagicMock()
            cw_cog._load_inventory = AsyncMock(return_value=[])
            cw_cog._save_inventory = AsyncMock(return_value=True)
            cog.bot.cogs["CyberwareShop"] = cw_cog
            inter = _make_interaction()
            await _process_store_add_cw(cog, inter, owner, "Kiroshi Optics, 3, 8000")
            cw_cog._save_inventory.assert_called_once()
            saved_inv = cw_cog._save_inventory.call_args[0][1]
            assert len(saved_inv) == 3
            assert all(i["name"] == "Kiroshi Optics" for i in saved_inv)
            assert "Kiroshi Optics" in inter.followup.send.call_args[0][0]
        _run(_test())

    @patch("NightCityBot.cogs.fixer_hub._audit_channel", new_callable=AsyncMock, return_value=None)
    def test_store_remove_gun_success(self, mock_audit):
        async def _test():
            from NightCityBot.cogs.fixer_hub import _process_store_remove_gun
            cog = _make_cog()
            owner = _make_member(222, "ShopOwner")
            guns_cog = MagicMock()
            state = {"stores": {"999:222": {"lots": [
                {"lot_id": "lot-1", "gun_name": "Pistol", "qty_remaining": 3},
            ]}}}
            guns_cog._load_state = AsyncMock(return_value=state)
            guns_cog._save_state = AsyncMock(return_value=True)
            guns_cog._store_id = MagicMock(return_value="999:222")
            guns_cog.lock = asyncio.Lock()
            cog.bot.cogs["GunsShopCog"] = guns_cog
            inter = _make_interaction()
            await _process_store_remove_gun(cog, inter, owner, "lot-1")
            assert "Removed" in inter.followup.send.call_args[0][0]
            assert "Pistol" in inter.followup.send.call_args[0][0]
        _run(_test())

    @patch("NightCityBot.cogs.fixer_hub._audit_channel", new_callable=AsyncMock, return_value=None)
    def test_store_remove_cw_success(self, mock_audit):
        async def _test():
            from NightCityBot.cogs.fixer_hub import _process_store_remove_cw
            cog = _make_cog()
            owner = _make_member(222, "Doc")
            cw_cog = MagicMock()
            cw_cog._load_inventory = AsyncMock(return_value=[
                {"item_id": "abc-123", "name": "Sandevistan", "price_paid": 5000},
            ])
            cw_cog._save_inventory = AsyncMock(return_value=True)
            cog.bot.cogs["CyberwareShop"] = cw_cog
            inter = _make_interaction()
            await _process_store_remove_cw(cog, inter, owner, "abc-123")
            assert "Removed" in inter.followup.send.call_args[0][0]
            assert "Sandevistan" in inter.followup.send.call_args[0][0]
        _run(_test())


class TestWholesaleProcessFunctions:
    @patch("NightCityBot.cogs.fixer_hub._audit_channel", new_callable=AsyncMock, return_value=None)
    def test_add_gun_to_wholesale(self, mock_audit):
        async def _test():
            from NightCityBot.cogs.fixer_hub import _process_wh_add_gun
            cog = _make_cog()
            guns_cog = MagicMock()
            state = {"wholesale_lots": []}
            guns_cog._load_state = AsyncMock(return_value=state)
            guns_cog._save_state = AsyncMock(return_value=True)
            guns_cog.lock = asyncio.Lock()
            cog.bot.cogs["GunsShopCog"] = guns_cog
            inter = _make_interaction()
            msg = MagicMock()
            msg.edit = AsyncMock()
            await _process_wh_add_gun(cog, inter, "TestGun, 10, 5000, basic", msg)
            guns_cog._save_state.assert_called_once()
            assert len(state["wholesale_lots"]) == 1
            assert "TestGun" in msg.edit.call_args[1].get("content", "")
        _run(_test())

    @patch("NightCityBot.cogs.fixer_hub._audit_channel", new_callable=AsyncMock, return_value=None)
    def test_add_cw_to_wholesale(self, mock_audit):
        async def _test():
            from NightCityBot.cogs.fixer_hub import _process_wh_add_cw
            cog = _make_cog()
            cw_cog = MagicMock()
            state = {"cw_wholesale_lots": []}
            cw_cog._load_state = AsyncMock(return_value=state)
            cw_cog._save_state = AsyncMock(return_value=True)
            cw_cog.lock = asyncio.Lock()
            cog.bot.cogs["CyberwareShop"] = cw_cog
            inter = _make_interaction()
            msg = MagicMock()
            msg.edit = AsyncMock()
            await _process_wh_add_cw(cog, inter, "Sandevistan, 5, 8000", msg)
            cw_cog._save_state.assert_called_once()
            assert len(state["cw_wholesale_lots"]) == 1
            assert "Sandevistan" in msg.edit.call_args[1].get("content", "")
        _run(_test())

    @patch("NightCityBot.cogs.fixer_hub._audit_channel", new_callable=AsyncMock, return_value=None)
    def test_remove_gun_lot(self, mock_audit):
        async def _test():
            from NightCityBot.cogs.fixer_hub import _process_wh_remove_lot
            cog = _make_cog()
            guns_cog = MagicMock()
            state = {"wholesale_lots": [
                {"lot_id": "lot-g1", "gun_name": "Pistol", "qty_available": 5},
            ]}
            guns_cog._load_state = AsyncMock(return_value=state)
            guns_cog._save_state = AsyncMock(return_value=True)
            guns_cog.lock = asyncio.Lock()
            cog.bot.cogs["GunsShopCog"] = guns_cog
            inter = _make_interaction()
            await _process_wh_remove_lot(cog, inter, "lot-g1")
            assert len(state["wholesale_lots"]) == 0
            assert "Removed" in inter.followup.send.call_args.kwargs.get("content", "")
        _run(_test())

    @patch("NightCityBot.cogs.fixer_hub._audit_channel", new_callable=AsyncMock, return_value=None)
    def test_remove_cw_lot(self, mock_audit):
        async def _test():
            from NightCityBot.cogs.fixer_hub import _process_wh_remove_lot
            cog = _make_cog()
            cw_cog = MagicMock()
            state = {"cw_wholesale_lots": [
                {"lot_id": "lot-cw1", "item_name": "Kiroshi", "qty_available": 3},
            ]}
            cw_cog._load_state = AsyncMock(return_value=state)
            cw_cog._save_state = AsyncMock(return_value=True)
            cw_cog.lock = asyncio.Lock()
            cog.bot.cogs["CyberwareShop"] = cw_cog
            inter = _make_interaction()
            await _process_wh_remove_lot(cog, inter, "lot-cw1")
            assert len(state["cw_wholesale_lots"]) == 0
            assert "Kiroshi" in inter.followup.send.call_args.kwargs.get("content", "")
        _run(_test())

    def test_remove_lot_not_found(self):
        async def _test():
            from NightCityBot.cogs.fixer_hub import _process_wh_remove_lot
            cog = _make_cog()
            guns_cog = MagicMock()
            guns_cog._load_state = AsyncMock(return_value={"wholesale_lots": []})
            cog.bot.cogs["GunsShopCog"] = guns_cog
            inter = _make_interaction()
            await _process_wh_remove_lot(cog, inter, "nope")
            assert "not found" in inter.followup.send.call_args.kwargs.get("content", "")
        _run(_test())

    def test_add_gun_bad_input(self):
        async def _test():
            from NightCityBot.cogs.fixer_hub import _process_wh_add_gun
            cog = _make_cog()
            guns_cog = MagicMock()
            cog.bot.cogs["GunsShopCog"] = guns_cog
            inter = _make_interaction()
            msg = MagicMock()
            msg.edit = AsyncMock()
            await _process_wh_add_gun(cog, inter, "TestGun, abc, xyz", msg)
            assert "numbers" in msg.edit.call_args[1].get("content", "").lower()
        _run(_test())


def _make_guns_cog(state_dict):
    guns_cog = MagicMock()
    guns_cog.lock = asyncio.Lock()
    guns_cog._load_state = AsyncMock(return_value=state_dict)
    guns_cog._save_state = AsyncMock()
    return guns_cog


def test_store_remove_gun_lot_not_found():
    async def _test():
        from NightCityBot.cogs.fixer_hub import _process_store_remove_gun
        cog = _make_cog()
        store_key = "999:111"
        owner = _make_member(111, "TestOwner")
        guns_cog = _make_guns_cog({
            "stores": {
                store_key: {
                    "lots": [{"lot_id": "L1", "gun_name": "Pistol", "qty_remaining": 5}]
                }
            }
        })
        guns_cog._store_id = MagicMock(return_value=store_key)
        cog.bot.cogs["GunsShopCog"] = guns_cog
        inter = _make_interaction()
        await _process_store_remove_gun(cog, inter, owner, "NONEXISTENT")
        assert "not found" in inter.followup.send.call_args[0][0].lower()
    _run(_test())


def test_store_remove_cw_item_not_found():
    async def _test():
        from NightCityBot.cogs.fixer_hub import _process_store_remove_cw
        cog = _make_cog()
        owner = _make_member(111, "TestOwner")
        cw_cog = MagicMock()
        cw_cog._load_inventory = AsyncMock(return_value=[
            {"item_id": "abc-1", "name": "Sandevistan", "price_paid": 5000},
        ])
        cog.bot.cogs["CyberwareShop"] = cw_cog
        inter = _make_interaction()
        await _process_store_remove_cw(cog, inter, owner, "NONEXISTENT")
        assert "not found" in inter.followup.send.call_args[0][0].lower()
    _run(_test())




class TestPickerUserSelectCallbacks:
    @patch("NightCityBot.cogs.fixer_hub.get_active_characters", new_callable=AsyncMock, return_value=[{"character_id": "char-1", "name": "V", "status": "active"}])
    def test_add_item_picker_select_sets_player(self, mock_chars):
        async def _test():
            cog = _make_cog()
            ctx = _ctx()
            view = PlayerAddItemPickerView(cog, ctx)
            select = _find_user_select(view)
            member = _make_member(222, "TestPlayer")
            select._values = [member]
            inter = _make_interaction()
            await select.callback(inter)
            assert view.selected_player is member
            inter.response.edit_message.assert_called_once()
            assert "TestPlayer" in inter.response.edit_message.call_args.kwargs["content"]
        _run(_test())

    def test_store_owner_picker_selects_member(self):
        async def _test():
            cog = _make_cog()
            ctx = _ctx()
            owner = _make_member(222, "ShopOwner")
            ctx.guild.get_member = MagicMock(return_value=owner)
            guns_cog = MagicMock()
            guns_cog._load_state = AsyncMock(return_value={"stores": {}})
            guns_cog._store_id = MagicMock(return_value="999:222")
            cog.bot.cogs["GunsShopCog"] = guns_cog
            options = [discord.SelectOption(label="ShopOwner", value="222")]
            view = StoreOwnerPickerView(cog, ctx, options, store_type="gun")
            inter = _make_interaction()
            inter.data = {"values": ["222"]}
            await view._on_select(inter)
            inter.response.defer.assert_called_once()
        _run(_test())

    def test_store_remove_lot_picker_selects_lot(self):
        async def _test():
            from NightCityBot.cogs.fixer_hub import _process_store_remove_gun
            cog = _make_cog()
            ctx = _ctx()
            owner = _make_member(222, "ShopOwner")
            guns_cog = MagicMock()
            state = {"stores": {"999:222": {"lots": [
                {"lot_id": "lot-1", "gun_name": "Pistol", "qty_remaining": 3},
            ]}}}
            guns_cog._load_state = AsyncMock(return_value=state)
            guns_cog._save_state = AsyncMock(return_value=True)
            guns_cog._store_id = MagicMock(return_value="999:222")
            guns_cog.lock = asyncio.Lock()
            cog.bot.cogs["GunsShopCog"] = guns_cog
            options = [discord.SelectOption(label="Pistol", value="lot-1")]
            view = StoreRemoveLotPickerView(cog, ctx, owner, options, store_type="gun")
            inter = _make_interaction()
            inter.data = {"values": ["lot-1"]}
            await view._on_select(inter)
            assert "Removed" in inter.followup.send.call_args[0][0]
        _run(_test())


class TestPickerFailureBranches:
    def test_loa_picker_cog_missing(self):
        async def _test():
            cog = _make_cog()
            ctx = _ctx()
            cog.bot.get_cog = MagicMock(return_value=None)
            member = _make_member(222, "TestPlayer")
            view = LOAPickerView(cog, ctx, action="start")
            select = _find_user_select(view)
            select._values = [member]
            inter = _make_interaction()
            await select.callback(inter)
            assert "unavailable" in inter.followup.send.call_args[0][0].lower()
        _run(_test())

    def test_loa_picker_role_missing(self):
        async def _test():
            cog = _make_cog()
            ctx = _ctx()
            loa_cog = MagicMock()
            loa_cog.get_loa_role = MagicMock(return_value=None)
            cog.bot.get_cog = MagicMock(return_value=loa_cog)
            member = _make_member(222, "TestPlayer")
            view = LOAPickerView(cog, ctx, action="start")
            select = _find_user_select(view)
            select._values = [member]
            inter = _make_interaction()
            await select.callback(inter)
            assert "not configured" in inter.followup.send.call_args[0][0].lower()
        _run(_test())

    def test_store_owner_picker_gun_cog_missing(self):
        async def _test():
            cog = _make_cog()
            ctx = _ctx()
            owner = _make_member(222, "ShopOwner")
            ctx.guild.get_member = MagicMock(return_value=owner)
            options = [discord.SelectOption(label="ShopOwner", value="222")]
            view = StoreOwnerPickerView(cog, ctx, options, store_type="gun")
            inter = _make_interaction()
            inter.data = {"values": ["222"]}
            await view._on_select(inter)
            assert "unavailable" in inter.followup.send.call_args[0][0].lower()
        _run(_test())

    def test_store_owner_picker_cw_cog_missing(self):
        async def _test():
            cog = _make_cog()
            ctx = _ctx()
            owner = _make_member(222, "Doc")
            ctx.guild.get_member = MagicMock(return_value=owner)
            options = [discord.SelectOption(label="Doc", value="222")]
            view = StoreOwnerPickerView(cog, ctx, options, store_type="cw")
            inter = _make_interaction()
            inter.data = {"values": ["222"]}
            await view._on_select(inter)
            assert "unavailable" in inter.followup.send.call_args[0][0].lower()
        _run(_test())

    def test_store_owner_picker_cw_empty(self):
        async def _test():
            cog = _make_cog()
            ctx = _ctx()
            owner = _make_member(222, "Doc")
            ctx.guild.get_member = MagicMock(return_value=owner)
            cw_cog = MagicMock()
            cw_cog._load_state = AsyncMock(return_value={"ripperdoc_stores": {}})
            cw_cog._load_inventory = AsyncMock(return_value=[])
            cog.bot.cogs["CyberwareShop"] = cw_cog
            options = [discord.SelectOption(label="Doc", value="222")]
            view = StoreOwnerPickerView(cog, ctx, options, store_type="cw")
            inter = _make_interaction()
            inter.data = {"values": ["222"]}
            await view._on_select(inter)
            embed = inter.followup.send.call_args.kwargs.get("embed")
            assert embed is not None
            assert "empty" in embed.description.lower()
        _run(_test())


MOCK_PLAYER_INVENTORY = [
    {"item_id": "uuid-a1", "owner_id": "200", "character_name": "V",
     "name": "Katana", "item_type": "melee", "price_paid": 500,
     "seller_name": "Shop", "acquired_at": "2025-01-01"},
    {"item_id": "uuid-a2", "owner_id": "200", "character_name": "V",
     "name": "Katana", "item_type": "melee", "price_paid": 500,
     "seller_name": "Shop", "acquired_at": "2025-01-01"},
    {"item_id": "uuid-b1", "owner_id": "200", "character_name": "V",
     "name": "Pistol", "item_type": "gun", "price_paid": 1000,
     "seller_name": "Dealer", "acquired_at": "2025-02-01"},
]


class TestRemoveItemDropdownFlow:
    """Regression: Fixer Remove Item uses inventory dropdown, not UUID text."""

    def test_player_select_sets_member(self):
        async def _test():
            cog = _make_cog()
            ctx = _ctx()
            view = PlayerRemoveItemView(cog, ctx)
            inter = _make_interaction()
            member = _make_member(200, "TargetPlayer")
            select = _find_user_select(view)
            select._values = [member]
            ctx.guild.get_member = MagicMock(return_value=member)
            await select.callback(inter)
            assert view.selected_player == member
        _run(_test())

    def test_continue_no_player_selected(self):
        async def _test():
            cog = _make_cog()
            ctx = _ctx()
            view = PlayerRemoveItemView(cog, ctx)
            inter = _make_interaction()
            btn = _find_button(view, "Continue →")
            await btn.callback(inter)
            msg = inter.response.send_message.call_args[0][0]
            assert "select a player" in msg.lower()
        _run(_test())

    @patch("NightCityBot.cogs.fixer_hub.pi_get_by_owner", new_callable=AsyncMock, return_value=[])
    def test_continue_empty_inventory(self, mock_get):
        async def _test():
            cog = _make_cog()
            ctx = _ctx()
            view = PlayerRemoveItemView(cog, ctx)
            view.selected_player = _make_member(200, "TargetPlayer")
            inter = _make_interaction()
            btn = _find_button(view, "Continue →")
            await btn.callback(inter)
            msg = inter.followup.send.call_args[0][0]
            assert "no items" in msg.lower()
        _run(_test())

    @patch("NightCityBot.cogs.fixer_hub.pi_get_by_owner", new_callable=AsyncMock)
    def test_continue_shows_inventory_dropdown(self, mock_get):
        mock_get.return_value = MOCK_PLAYER_INVENTORY

        async def _test():
            cog = _make_cog()
            ctx = _ctx()
            view = PlayerRemoveItemView(cog, ctx)
            view.selected_player = _make_member(200, "TargetPlayer")
            inter = _make_interaction()
            btn = _find_button(view, "Continue →")
            await btn.callback(inter)
            kwargs = inter.followup.send.call_args.kwargs
            step2_view = kwargs["view"]
            assert isinstance(step2_view, RemoveItemPickerView)
            options = step2_view.item_dropdown.options
            labels = [o.label for o in options]
            assert any("Katana" in l for l in labels)
            assert any("Pistol" in l for l in labels)
            katana_opt = [o for o in options if "Katana" in o.label][0]
            assert "×2" in katana_opt.label
        _run(_test())

    @patch("NightCityBot.cogs.fixer_hub.ih_record_event", new_callable=AsyncMock)
    @patch("NightCityBot.cogs.fixer_hub.pi_delete_item", new_callable=AsyncMock, return_value=True)
    @patch("NightCityBot.cogs.fixer_hub.pi_get_item", new_callable=AsyncMock)
    def test_single_item_removal(self, mock_get_item, mock_delete, mock_event):
        mock_get_item.return_value = {"item_id": "uuid-b1", "owner_id": "200", "name": "Pistol"}

        async def _test():
            cog = _make_cog()
            ctx = _ctx()
            player = _make_member(200, "TargetPlayer")
            grouped = {"Pistol": [MOCK_PLAYER_INVENTORY[2]]}
            view = RemoveItemPickerView(cog, ctx, player, grouped)
            view.item_dropdown.options = [discord.SelectOption(label="Pistol", value="Pistol")]
            inter = _make_interaction()
            inter.data = {"values": ["Pistol"]}
            view.item_dropdown._values = ["Pistol"]
            await view.item_dropdown.callback(inter)
            mock_delete.assert_called_once_with("uuid-b1")
            msg = inter.followup.send.call_args[0][0]
            assert "Pistol" in msg
            assert "Removed" in msg
        _run(_test())

    @patch("NightCityBot.cogs.fixer_hub.ih_record_event", new_callable=AsyncMock)
    @patch("NightCityBot.cogs.fixer_hub.pi_delete_item", new_callable=AsyncMock, return_value=True)
    @patch("NightCityBot.cogs.fixer_hub.pi_get_item", new_callable=AsyncMock)
    @patch("NightCityBot.cogs.fixer_hub.collect_text_input", new_callable=AsyncMock, return_value="2")
    def test_multi_quantity_removal(self, mock_text, mock_get_item, mock_delete, mock_event):
        mock_get_item.return_value = {"item_id": "uuid-a1", "owner_id": "200", "name": "Katana"}

        async def _test():
            cog = _make_cog()
            ctx = _ctx()
            player = _make_member(200, "TargetPlayer")
            grouped = {"Katana": MOCK_PLAYER_INVENTORY[:2]}
            view = RemoveItemPickerView(cog, ctx, player, grouped)
            view.item_dropdown.options = [discord.SelectOption(label="Katana ×2", value="Katana")]
            inter = _make_interaction()
            inter.data = {"values": ["Katana"]}
            inter.channel_id = 123
            view.item_dropdown._values = ["Katana"]
            await view.item_dropdown.callback(inter)
            assert mock_delete.call_count == 2
            msg = inter.followup.send.call_args[0][0]
            assert "Katana" in msg
            assert "×2" in msg
        _run(_test())

    @patch("NightCityBot.cogs.fixer_hub.collect_text_input", new_callable=AsyncMock, return_value=None)
    def test_multi_quantity_timeout(self, mock_text):
        async def _test():
            cog = _make_cog()
            ctx = _ctx()
            player = _make_member(200, "TargetPlayer")
            grouped = {"Katana": MOCK_PLAYER_INVENTORY[:2]}
            view = RemoveItemPickerView(cog, ctx, player, grouped)
            view.item_dropdown.options = [discord.SelectOption(label="Katana ×2", value="Katana")]
            inter = _make_interaction()
            inter.data = {"values": ["Katana"]}
            inter.channel_id = 123
            view.item_dropdown._values = ["Katana"]
            await view.item_dropdown.callback(inter)
            msg = inter.followup.send.call_args[0][0]
            assert "timed out" in msg.lower() or "cancelled" in msg.lower()
        _run(_test())

    @patch("NightCityBot.cogs.fixer_hub.collect_text_input", new_callable=AsyncMock, return_value="0")
    def test_multi_quantity_invalid_range(self, mock_text):
        async def _test():
            cog = _make_cog()
            ctx = _ctx()
            player = _make_member(200, "TargetPlayer")
            grouped = {"Katana": MOCK_PLAYER_INVENTORY[:2]}
            view = RemoveItemPickerView(cog, ctx, player, grouped)
            view.item_dropdown.options = [discord.SelectOption(label="Katana ×2", value="Katana")]
            inter = _make_interaction()
            inter.data = {"values": ["Katana"]}
            inter.channel_id = 123
            view.item_dropdown._values = ["Katana"]
            await view.item_dropdown.callback(inter)
            msg = inter.followup.send.call_args[0][0]
            assert "between 1 and 2" in msg.lower()
        _run(_test())

    @patch("NightCityBot.cogs.fixer_hub.ih_record_event", new_callable=AsyncMock)
    @patch("NightCityBot.cogs.fixer_hub.pi_delete_item", new_callable=AsyncMock, return_value=True)
    @patch("NightCityBot.cogs.fixer_hub.pi_get_item", new_callable=AsyncMock)
    def test_uses_item_id_not_id(self, mock_get_item, mock_delete, mock_event):
        """Regression: _do_remove must use item.get('item_id'), not item.get('id')."""
        item = {"item_id": "real-uuid-123", "owner_id": "200", "name": "Katana"}
        mock_get_item.return_value = item

        async def _test():
            cog = _make_cog()
            ctx = _ctx()
            player = _make_member(200, "TargetPlayer")
            grouped = {"Katana": [item]}
            view = RemoveItemPickerView(cog, ctx, player, grouped)
            view.item_dropdown.options = [discord.SelectOption(label="Katana", value="Katana")]
            inter = _make_interaction()
            inter.data = {"values": ["Katana"]}
            view.item_dropdown._values = ["Katana"]
            await view.item_dropdown.callback(inter)
            mock_get_item.assert_called_with("real-uuid-123")
            mock_delete.assert_called_with("real-uuid-123")
        _run(_test())

    @patch("NightCityBot.cogs.fixer_hub.pi_delete_item", new_callable=AsyncMock, return_value=True)
    @patch("NightCityBot.cogs.fixer_hub.pi_get_item", new_callable=AsyncMock, return_value=None)
    def test_stale_item_skipped(self, mock_get_item, mock_delete):
        """Regression: items that vanish between select and delete are skipped."""
        async def _test():
            cog = _make_cog()
            ctx = _ctx()
            player = _make_member(200, "TargetPlayer")
            grouped = {"Katana": [MOCK_PLAYER_INVENTORY[0]]}
            view = RemoveItemPickerView(cog, ctx, player, grouped)
            view.item_dropdown.options = [discord.SelectOption(label="Katana", value="Katana")]
            inter = _make_interaction()
            inter.data = {"values": ["Katana"]}
            view.item_dropdown._values = ["Katana"]
            await view.item_dropdown.callback(inter)
            mock_delete.assert_not_called()
            msg = inter.followup.send.call_args[0][0]
            assert "failed" in msg.lower()
        _run(_test())

    def test_interaction_check_blocks_other_users(self):
        async def _test():
            cog = _make_cog()
            ctx = _ctx(author_id=111)
            player = _make_member(200, "TargetPlayer")
            grouped = {"Katana": MOCK_PLAYER_INVENTORY[:1]}
            view = RemoveItemPickerView(cog, ctx, player, grouped)
            inter = _make_interaction(user_id=999)
            result = await view.interaction_check(inter)
            assert result is False
        _run(_test())


class TestFixerItemHistoryViews:
    def test_source_view_player_button(self):
        from NightCityBot.cogs.fixer_hub import FixerItemHistorySourceView, FixerItemHistoryPlayerPickerView
        async def _test():
            cog = _make_cog()
            ctx = _ctx()
            view = FixerItemHistorySourceView(cog, ctx)
            inter = _make_interaction()
            btn = _find_button(view, "Player Item")
            await btn.callback(inter)
            args = inter.response.edit_message.call_args
            assert isinstance(args.kwargs["view"], FixerItemHistoryPlayerPickerView)
        _run(_test())

    def test_source_view_store_button(self):
        from NightCityBot.cogs.fixer_hub import FixerItemHistorySourceView, FixerItemHistoryStorePickerView
        async def _test():
            cog = _make_cog()
            ctx = _ctx()
            view = FixerItemHistorySourceView(cog, ctx)
            inter = _make_interaction()
            btn = _find_button(view, "Store Item")
            await btn.callback(inter)
            args = inter.response.edit_message.call_args
            assert isinstance(args.kwargs["view"], FixerItemHistoryStorePickerView)
        _run(_test())

    def test_source_view_blocks_wrong_user(self):
        from NightCityBot.cogs.fixer_hub import FixerItemHistorySourceView
        async def _test():
            cog = _make_cog()
            ctx = _ctx(author_id=42)
            view = FixerItemHistorySourceView(cog, ctx)
            inter = _make_interaction(user_id=999)
            result = await view.interaction_check(inter)
            assert result is False
        _run(_test())

    @patch("NightCityBot.cogs.fixer_hub.pi_get_by_owner", new_callable=AsyncMock, return_value=[])
    def test_player_picker_no_items(self, mock_get):
        from NightCityBot.cogs.fixer_hub import FixerItemHistoryPlayerPickerView
        async def _test():
            cog = _make_cog()
            member_mock = _make_member(100, "TestPlayer")
            ctx = _ctx()
            ctx.guild.get_member = MagicMock(return_value=member_mock)
            view = FixerItemHistoryPlayerPickerView(cog, ctx)
            inter = _make_interaction()
            sel = [c for c in view.children if isinstance(c, discord.ui.UserSelect)][0]
            sel._values = [member_mock]
            await sel.callback(inter)
            inter.followup.send.assert_called_once()
            msg = inter.followup.send.call_args.kwargs.get("content", inter.followup.send.call_args[0][0])
            assert "no items" in msg.lower()
        _run(_test())

    @patch("NightCityBot.cogs.fixer_hub.ih_get_history", new_callable=AsyncMock, return_value=[
        {"created_at": "2025-01-01T00:00:00", "event_type": "purchase", "actor_id": "42", "target_id": "", "price": 500, "metadata": {"item_name": "Gun"}}
    ])
    def test_item_picker_shows_embed(self, mock_hist):
        from NightCityBot.cogs.fixer_hub import FixerItemHistoryItemPickerView
        async def _test():
            cog = _make_cog()
            ctx = _ctx()
            options = [discord.SelectOption(label="Test Gun", value="uuid-1234")]
            view = FixerItemHistoryItemPickerView(cog, ctx, options, "TestPlayer")
            inter = _make_interaction()
            inter.data = {"values": ["uuid-1234"]}
            await view._on_select(inter)
            kwargs = inter.followup.send.call_args.kwargs
            assert "embed" in kwargs
            assert kwargs["embed"].title.startswith("📜 Item History")
        _run(_test())


class TestStoreNicknameInFixerHub:
    def test_gun_store_dropdown_uses_store_name(self):
        async def _test():
            cog = _make_cog()
            ctx = _ctx()
            member1 = _make_member(200, "GunGuy")
            role = MagicMock()
            role.members = [member1]
            ctx.guild.get_role = MagicMock(side_effect=lambda rid: role if rid == GUN_STORE_OWNER_ROLE_ID else None)
            guns_cog = MagicMock()
            guns_cog._store_id = MagicMock(return_value="999:200")
            guns_cog._load_state = AsyncMock(return_value={
                "stores": {"999:200": {"owner_id": 200, "store_name": "Hellfire Arms", "lots": []}}
            })
            cog.bot.cogs["GunsShopCog"] = guns_cog
            view = StoreSubView(cog, ctx)
            inter = _make_interaction()
            btn = _find_button(view, "View Gun Store")
            await btn.callback(inter)
            kwargs = inter.followup.send.call_args.kwargs
            picker = kwargs["view"]
            assert isinstance(picker, StoreOwnerPickerView)
            select = picker.children[0]
            assert select.options[0].label == "Hellfire Arms"
        _run(_test())

    def test_gun_store_dropdown_fallback_to_display_name(self):
        async def _test():
            cog = _make_cog()
            ctx = _ctx()
            member1 = _make_member(200, "GunGuy")
            role = MagicMock()
            role.members = [member1]
            ctx.guild.get_role = MagicMock(side_effect=lambda rid: role if rid == GUN_STORE_OWNER_ROLE_ID else None)
            guns_cog = MagicMock()
            guns_cog._store_id = MagicMock(return_value="999:200")
            guns_cog._load_state = AsyncMock(return_value={"stores": {}})
            cog.bot.cogs["GunsShopCog"] = guns_cog
            view = StoreSubView(cog, ctx)
            inter = _make_interaction()
            btn = _find_button(view, "View Gun Store")
            await btn.callback(inter)
            kwargs = inter.followup.send.call_args.kwargs
            picker = kwargs["view"]
            select = picker.children[0]
            assert select.options[0].label == "GunGuy's Gun Store"
        _run(_test())


class TestFixerHubEmployeeVisibility:
    def test_gun_store_embed_shows_employees(self):
        async def _test():
            cog = _make_cog()
            ctx = _ctx()
            owner = _make_member(200, "GunGuy")
            ctx.guild.get_member = MagicMock(return_value=owner)
            guns_cog = MagicMock()
            guns_cog._store_id = MagicMock(return_value="999:200")
            guns_cog._load_state = AsyncMock(return_value={
                "stores": {
                    "999:200": {
                        "owner_id": 200,
                        "store_name": "Hellfire Arms",
                        "employees": [301, 302],
                        "lots": [{"gun_name": "Pistol", "unit_cost": 100, "qty_remaining": 1, "restriction": "basic"}],
                    }
                }
            })
            cog.bot.cogs["GunsShopCog"] = guns_cog
            options = [discord.SelectOption(label="GunGuy", value="200")]
            view = StoreOwnerPickerView(cog, ctx, options, store_type="gun")
            inter = _make_interaction()
            inter.data = {"values": ["200"]}
            await view._on_select(inter)
            embed = inter.followup.send.call_args.kwargs["embed"]
            assert "Hellfire Arms" in embed.title
            field_names = [f.name for f in embed.fields]
            assert any("Employees" in n for n in field_names)
            emp_field = [f for f in embed.fields if "Employees" in f.name][0]
            assert "<@301>" in emp_field.value
            assert "<@302>" in emp_field.value
        _run(_test())

    def test_gun_store_embed_no_employees_no_field(self):
        async def _test():
            cog = _make_cog()
            ctx = _ctx()
            owner = _make_member(200, "GunGuy")
            ctx.guild.get_member = MagicMock(return_value=owner)
            guns_cog = MagicMock()
            guns_cog._store_id = MagicMock(return_value="999:200")
            guns_cog._load_state = AsyncMock(return_value={
                "stores": {
                    "999:200": {
                        "owner_id": 200,
                        "lots": [{"gun_name": "Pistol", "unit_cost": 100, "qty_remaining": 1, "restriction": "basic"}],
                    }
                }
            })
            cog.bot.cogs["GunsShopCog"] = guns_cog
            options = [discord.SelectOption(label="GunGuy", value="200")]
            view = StoreOwnerPickerView(cog, ctx, options, store_type="gun")
            inter = _make_interaction()
            inter.data = {"values": ["200"]}
            await view._on_select(inter)
            embed = inter.followup.send.call_args.kwargs["embed"]
            field_names = [f.name for f in embed.fields]
            assert not any("Employees" in n for n in field_names)
        _run(_test())
