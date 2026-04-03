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
    PlayerInvModal,
    PlayerAddItemModal,
    PlayerRemoveItemModal,
    PlayerReassignModal,
    ItemHistoryModal,
    LOAModal,
    StoreInvModal,
    StoreAddModal,
    StoreRemoveModal,
    WHAddGunModal,
    WHAddCWModal,
    WHRemoveLotModal,
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


def _make_interaction(user_id=111):
    inter = MagicMock(spec=discord.Interaction)
    inter.user = MagicMock(spec=discord.Member)
    inter.user.id = user_id
    inter.user.display_name = f"User{user_id}"
    inter.user.mention = f"<@{user_id}>"
    inter.response = MagicMock()
    inter.response.defer = AsyncMock()
    inter.response.send_message = AsyncMock()
    inter.response.send_modal = AsyncMock()
    inter.response.edit_message = AsyncMock()
    inter.followup = MagicMock()
    inter.followup.send = AsyncMock()
    inter.guild = MagicMock()
    inter.guild.id = 999
    inter.guild.get_member = MagicMock(return_value=None)
    inter.guild.fetch_member = AsyncMock(return_value=None)
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


def _make_cog():
    bot = _make_bot()
    cog = FixerHubCog.__new__(FixerHubCog)
    cog.bot = bot
    return cog


def _run(coro):
    return asyncio.run(coro)


class TestFixerCommand:
    def test_dm_guard(self):
        cog = _make_cog()
        ctx = _ctx(guild=False)
        _run(cog.fixer.callback(cog, ctx))
        ctx.send.assert_called_once()
        assert "server" in ctx.send.call_args[0][0].lower()

    def test_opens_top_view(self):
        cog = _make_cog()
        ctx = _ctx()
        _run(cog.fixer.callback(cog, ctx))
        ctx.send.assert_called_once()
        kwargs = ctx.send.call_args.kwargs
        assert isinstance(kwargs["view"], FixerTopView)
        assert "Fixer Panel" in kwargs["embed"].title


class TestTopViewNavigation:
    def test_player_button_edits(self):
        async def _test():
            cog = _make_cog()
            ctx = _ctx()
            view = FixerTopView(cog, ctx)
            inter = _make_interaction()
            btn = _find_button(view, "Player")
            await btn.callback(inter)
            inter.response.edit_message.assert_called_once()
            kwargs = inter.response.edit_message.call_args.kwargs
            assert isinstance(kwargs["view"], PlayerSubView)
        _run(_test())

    def test_store_button_edits(self):
        async def _test():
            cog = _make_cog()
            ctx = _ctx()
            view = FixerTopView(cog, ctx)
            inter = _make_interaction()
            btn = _find_button(view, "Store")
            await btn.callback(inter)
            inter.response.edit_message.assert_called_once()
            kwargs = inter.response.edit_message.call_args.kwargs
            assert isinstance(kwargs["view"], StoreSubView)
        _run(_test())

    def test_wholesaler_button_edits(self):
        async def _test():
            cog = _make_cog()
            ctx = _ctx()
            view = FixerTopView(cog, ctx)
            inter = _make_interaction()
            btn = _find_button(view, "Wholesaler")
            await btn.callback(inter)
            inter.response.edit_message.assert_called_once()
            kwargs = inter.response.edit_message.call_args.kwargs
            assert isinstance(kwargs["view"], WholesalerSubView)
        _run(_test())

    def test_interaction_check_owner(self):
        async def _test():
            cog = _make_cog()
            ctx = _ctx(author_id=111)
            view = FixerTopView(cog, ctx)
            inter = _make_interaction(user_id=111)
            assert await view.interaction_check(inter)
        _run(_test())

    def test_interaction_check_other(self):
        async def _test():
            cog = _make_cog()
            ctx = _ctx(author_id=111)
            view = FixerTopView(cog, ctx)
            inter = _make_interaction(user_id=222)
            assert not await view.interaction_check(inter)
        _run(_test())


class TestPlayerSubViewButtons:
    def test_view_inventory_opens_modal(self):
        async def _test():
            cog = _make_cog()
            ctx = _ctx()
            parent = FixerTopView(cog, ctx)
            view = PlayerSubView(cog, ctx, parent)
            inter = _make_interaction()
            btn = _find_button(view, "View Inventory")
            await btn.callback(inter)
            inter.response.send_modal.assert_called_once()
            modal = inter.response.send_modal.call_args[0][0]
            assert isinstance(modal, PlayerInvModal)
        _run(_test())

    def test_add_item_opens_modal(self):
        async def _test():
            cog = _make_cog()
            ctx = _ctx()
            parent = FixerTopView(cog, ctx)
            view = PlayerSubView(cog, ctx, parent)
            inter = _make_interaction()
            btn = _find_button(view, "Add Item")
            await btn.callback(inter)
            modal = inter.response.send_modal.call_args[0][0]
            assert isinstance(modal, PlayerAddItemModal)
        _run(_test())

    def test_remove_item_opens_modal(self):
        async def _test():
            cog = _make_cog()
            ctx = _ctx()
            parent = FixerTopView(cog, ctx)
            view = PlayerSubView(cog, ctx, parent)
            inter = _make_interaction()
            btn = _find_button(view, "Remove Item")
            await btn.callback(inter)
            modal = inter.response.send_modal.call_args[0][0]
            assert isinstance(modal, PlayerRemoveItemModal)
        _run(_test())

    def test_reassign_opens_modal(self):
        async def _test():
            cog = _make_cog()
            ctx = _ctx()
            parent = FixerTopView(cog, ctx)
            view = PlayerSubView(cog, ctx, parent)
            inter = _make_interaction()
            btn = _find_button(view, "Reassign Item")
            await btn.callback(inter)
            modal = inter.response.send_modal.call_args[0][0]
            assert isinstance(modal, PlayerReassignModal)
        _run(_test())

    def test_item_history_opens_modal(self):
        async def _test():
            cog = _make_cog()
            ctx = _ctx()
            parent = FixerTopView(cog, ctx)
            view = PlayerSubView(cog, ctx, parent)
            inter = _make_interaction()
            btn = _find_button(view, "Item History")
            await btn.callback(inter)
            modal = inter.response.send_modal.call_args[0][0]
            assert isinstance(modal, ItemHistoryModal)
        _run(_test())

    def test_start_loa_opens_modal(self):
        async def _test():
            cog = _make_cog()
            ctx = _ctx()
            parent = FixerTopView(cog, ctx)
            view = PlayerSubView(cog, ctx, parent)
            inter = _make_interaction()
            btn = _find_button(view, "Start LOA")
            await btn.callback(inter)
            modal = inter.response.send_modal.call_args[0][0]
            assert isinstance(modal, LOAModal)
            assert modal.action == "start"
        _run(_test())

    def test_end_loa_opens_modal(self):
        async def _test():
            cog = _make_cog()
            ctx = _ctx()
            parent = FixerTopView(cog, ctx)
            view = PlayerSubView(cog, ctx, parent)
            inter = _make_interaction()
            btn = _find_button(view, "End LOA")
            await btn.callback(inter)
            modal = inter.response.send_modal.call_args[0][0]
            assert isinstance(modal, LOAModal)
            assert modal.action == "end"
        _run(_test())

    def test_back_button(self):
        async def _test():
            cog = _make_cog()
            ctx = _ctx()
            parent = FixerTopView(cog, ctx)
            view = PlayerSubView(cog, ctx, parent)
            inter = _make_interaction()
            btn = _find_button(view, "← Back")
            await btn.callback(inter)
            inter.response.edit_message.assert_called_once()
            kwargs = inter.response.edit_message.call_args.kwargs
            assert kwargs["view"] is parent
        _run(_test())


class TestStoreSubViewButtons:
    def test_view_gun_store_opens_modal(self):
        async def _test():
            cog = _make_cog()
            ctx = _ctx()
            parent = FixerTopView(cog, ctx)
            view = StoreSubView(cog, ctx, parent)
            inter = _make_interaction()
            btn = _find_button(view, "View Gun Store")
            await btn.callback(inter)
            modal = inter.response.send_modal.call_args[0][0]
            assert isinstance(modal, StoreInvModal)
            assert modal.store_type == "gun"
        _run(_test())

    def test_view_cw_store_opens_modal(self):
        async def _test():
            cog = _make_cog()
            ctx = _ctx()
            parent = FixerTopView(cog, ctx)
            view = StoreSubView(cog, ctx, parent)
            inter = _make_interaction()
            btn = _find_button(view, "View CW Store")
            await btn.callback(inter)
            modal = inter.response.send_modal.call_args[0][0]
            assert isinstance(modal, StoreInvModal)
            assert modal.store_type == "cw"
        _run(_test())

    def test_add_to_store_opens_modal(self):
        async def _test():
            cog = _make_cog()
            ctx = _ctx()
            parent = FixerTopView(cog, ctx)
            view = StoreSubView(cog, ctx, parent)
            inter = _make_interaction()
            btn = _find_button(view, "Add to Gun Store")
            await btn.callback(inter)
            modal = inter.response.send_modal.call_args[0][0]
            assert isinstance(modal, StoreAddModal)
        _run(_test())

    def test_remove_from_store_opens_modal(self):
        async def _test():
            cog = _make_cog()
            ctx = _ctx()
            parent = FixerTopView(cog, ctx)
            view = StoreSubView(cog, ctx, parent)
            inter = _make_interaction()
            btn = _find_button(view, "Remove from Gun Store")
            await btn.callback(inter)
            modal = inter.response.send_modal.call_args[0][0]
            assert isinstance(modal, StoreRemoveModal)
        _run(_test())

    def test_back_button(self):
        async def _test():
            cog = _make_cog()
            ctx = _ctx()
            parent = FixerTopView(cog, ctx)
            view = StoreSubView(cog, ctx, parent)
            inter = _make_interaction()
            btn = _find_button(view, "← Back")
            await btn.callback(inter)
            inter.response.edit_message.assert_called_once()
        _run(_test())


class TestWholesalerSubViewButtons:
    def test_view_stock_empty(self):
        async def _test():
            cog = _make_cog()
            ctx = _ctx()
            parent = FixerTopView(cog, ctx)
            view = WholesalerSubView(cog, ctx, parent)
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
            parent = FixerTopView(cog, ctx)
            view = WholesalerSubView(cog, ctx, parent)
            inter = _make_interaction()
            btn = _find_button(view, "View Stock")
            await btn.callback(inter)
            inter.followup.send.assert_called_once()
            embed = inter.followup.send.call_args.kwargs["embed"]
            assert "TestGun" in embed.description
        _run(_test())

    def test_add_gun_opens_modal(self):
        async def _test():
            cog = _make_cog()
            ctx = _ctx()
            parent = FixerTopView(cog, ctx)
            view = WholesalerSubView(cog, ctx, parent)
            inter = _make_interaction()
            btn = _find_button(view, "Add Gun")
            await btn.callback(inter)
            modal = inter.response.send_modal.call_args[0][0]
            assert isinstance(modal, WHAddGunModal)
        _run(_test())

    def test_add_cw_opens_modal(self):
        async def _test():
            cog = _make_cog()
            ctx = _ctx()
            parent = FixerTopView(cog, ctx)
            view = WholesalerSubView(cog, ctx, parent)
            inter = _make_interaction()
            btn = _find_button(view, "Add CW")
            await btn.callback(inter)
            modal = inter.response.send_modal.call_args[0][0]
            assert isinstance(modal, WHAddCWModal)
        _run(_test())

    def test_remove_lot_opens_modal(self):
        async def _test():
            cog = _make_cog()
            ctx = _ctx()
            parent = FixerTopView(cog, ctx)
            view = WholesalerSubView(cog, ctx, parent)
            inter = _make_interaction()
            btn = _find_button(view, "Remove Lot")
            await btn.callback(inter)
            modal = inter.response.send_modal.call_args[0][0]
            assert isinstance(modal, WHRemoveLotModal)
        _run(_test())

    def test_restock_guns_info(self):
        async def _test():
            cog = _make_cog()
            ctx = _ctx()
            parent = FixerTopView(cog, ctx)
            view = WholesalerSubView(cog, ctx, parent)
            inter = _make_interaction()
            btn = _find_button(view, "Restock Guns")
            await btn.callback(inter)
            inter.followup.send.assert_called_once()
            msg = inter.followup.send.call_args[0][0]
            assert "!guns_wh_restock" in msg
        _run(_test())

    def test_restock_cw_info(self):
        async def _test():
            cog = _make_cog()
            ctx = _ctx()
            parent = FixerTopView(cog, ctx)
            view = WholesalerSubView(cog, ctx, parent)
            inter = _make_interaction()
            btn = _find_button(view, "Restock CW")
            await btn.callback(inter)
            inter.followup.send.assert_called_once()
            msg = inter.followup.send.call_args[0][0]
            assert "!cw_wh_restock" in msg
        _run(_test())

    def test_back_button(self):
        async def _test():
            cog = _make_cog()
            ctx = _ctx()
            parent = FixerTopView(cog, ctx)
            view = WholesalerSubView(cog, ctx, parent)
            inter = _make_interaction()
            btn = _find_button(view, "← Back")
            await btn.callback(inter)
            inter.response.edit_message.assert_called_once()
        _run(_test())


class TestPlayerModals:
    @patch("NightCityBot.cogs.fixer_hub.pi_get_by_owner", new_callable=AsyncMock)
    @patch("NightCityBot.cogs.fixer_hub._resolve_member", new_callable=AsyncMock)
    def test_view_inventory_success(self, mock_resolve, mock_get):
        async def _test():
            cog = _make_cog()
            member = _make_member(222, "TestPlayer")
            mock_resolve.return_value = member
            mock_get.return_value = [
                {"item_type": "gun", "name": "Pistol", "character_name": "V", "item_id": "abc12345"},
            ]
            modal = PlayerInvModal(cog)
            modal.player_input = MagicMock(value="222")
            inter = _make_interaction()
            await modal.on_submit(inter)
            inter.followup.send.assert_called_once()
            embed = inter.followup.send.call_args.kwargs["embed"]
            assert "Pistol" in embed.description
        _run(_test())

    @patch("NightCityBot.cogs.fixer_hub._resolve_member", new_callable=AsyncMock)
    def test_view_inventory_not_found(self, mock_resolve):
        async def _test():
            cog = _make_cog()
            mock_resolve.return_value = None
            modal = PlayerInvModal(cog)
            modal.player_input = MagicMock(value="999")
            inter = _make_interaction()
            await modal.on_submit(inter)
            assert "Could not find" in inter.followup.send.call_args[0][0]
        _run(_test())

    @patch("NightCityBot.cogs.fixer_hub._audit_channel", new_callable=AsyncMock, return_value=None)
    @patch("NightCityBot.cogs.fixer_hub.ih_record_event", new_callable=AsyncMock)
    @patch("NightCityBot.cogs.fixer_hub.pi_add_item", new_callable=AsyncMock, return_value=True)
    @patch("NightCityBot.cogs.fixer_hub._resolve_member", new_callable=AsyncMock)
    def test_add_item_success(self, mock_resolve, mock_add, mock_record, mock_audit):
        async def _test():
            cog = _make_cog()
            member = _make_member(222, "TestPlayer")
            mock_resolve.return_value = member
            modal = PlayerAddItemModal(cog)
            modal.player_input = MagicMock(value="222")
            modal.name_input = MagicMock(value="Katana")
            modal.character_input = MagicMock(value="V")
            modal.item_type_input = MagicMock(value="gun")
            modal.qty_price_input = MagicMock(value="2,3000")
            inter = _make_interaction()
            await modal.on_submit(inter)
            assert mock_add.call_count == 2
            assert "Katana" in inter.followup.send.call_args[0][0]
        _run(_test())

    @patch("NightCityBot.cogs.fixer_hub._audit_channel", new_callable=AsyncMock, return_value=None)
    @patch("NightCityBot.cogs.fixer_hub.ih_record_event", new_callable=AsyncMock)
    @patch("NightCityBot.cogs.fixer_hub.pi_delete_item", new_callable=AsyncMock, return_value=True)
    @patch("NightCityBot.cogs.fixer_hub.pi_get_item", new_callable=AsyncMock)
    @patch("NightCityBot.cogs.fixer_hub._resolve_member", new_callable=AsyncMock)
    def test_remove_item_success(self, mock_resolve, mock_get, mock_del, mock_record, mock_audit):
        async def _test():
            cog = _make_cog()
            member = _make_member(222, "TestPlayer")
            mock_resolve.return_value = member
            item_id = str(uuid.uuid4())
            mock_get.return_value = {"owner_id": "222", "name": "Pistol"}
            modal = PlayerRemoveItemModal(cog)
            modal.player_input = MagicMock(value="222")
            modal.item_id_input = MagicMock(value=item_id)
            inter = _make_interaction()
            await modal.on_submit(inter)
            mock_del.assert_called_once_with(item_id)
            assert "Removed" in inter.followup.send.call_args[0][0]
        _run(_test())

    @patch("NightCityBot.cogs.fixer_hub.pi_get_item", new_callable=AsyncMock, return_value=None)
    @patch("NightCityBot.cogs.fixer_hub._resolve_member", new_callable=AsyncMock)
    def test_remove_item_not_found(self, mock_resolve, mock_get):
        async def _test():
            cog = _make_cog()
            mock_resolve.return_value = _make_member(222)
            modal = PlayerRemoveItemModal(cog)
            modal.player_input = MagicMock(value="222")
            modal.item_id_input = MagicMock(value="nope")
            inter = _make_interaction()
            await modal.on_submit(inter)
            assert "not found" in inter.followup.send.call_args[0][0]
        _run(_test())

    @patch("NightCityBot.cogs.fixer_hub._audit_channel", new_callable=AsyncMock, return_value=None)
    @patch("NightCityBot.cogs.fixer_hub.ih_record_event", new_callable=AsyncMock)
    @patch("NightCityBot.cogs.fixer_hub.pi_update_owner", new_callable=AsyncMock, return_value=True)
    @patch("NightCityBot.cogs.fixer_hub.pi_get_item", new_callable=AsyncMock)
    @patch("NightCityBot.cogs.fixer_hub._resolve_member", new_callable=AsyncMock)
    def test_reassign_success(self, mock_resolve, mock_get, mock_update, mock_record, mock_audit):
        async def _test():
            cog = _make_cog()
            new_owner = _make_member(333, "NewOwner")
            mock_resolve.return_value = new_owner
            item_id = str(uuid.uuid4())
            mock_get.return_value = {"owner_id": "222", "name": "Katana", "character_name": "OldChar"}
            modal = PlayerReassignModal(cog)
            modal.item_id_input = MagicMock(value=item_id)
            modal.player_input = MagicMock(value="333")
            modal.character_input = MagicMock(value="NewChar")
            inter = _make_interaction()
            await modal.on_submit(inter)
            mock_update.assert_called_once()
            assert "Reassigned" in inter.followup.send.call_args[0][0]
        _run(_test())

    @patch("NightCityBot.cogs.fixer_hub.ih_get_history", new_callable=AsyncMock)
    def test_item_history_success(self, mock_history):
        async def _test():
            cog = _make_cog()
            item_id = str(uuid.uuid4())
            mock_history.return_value = [
                {"created_at": "2025-01-01T12:00:00", "event_type": "admin_add", "actor_id": "111", "target_id": "222", "price": 5000, "metadata": {"item_name": "Pistol"}},
            ]
            modal = ItemHistoryModal(cog)
            modal.item_id_input = MagicMock(value=item_id)
            inter = _make_interaction()
            await modal.on_submit(inter)
            embed = inter.followup.send.call_args.kwargs["embed"]
            assert "admin_add" in embed.description
        _run(_test())

    @patch("NightCityBot.cogs.fixer_hub.ih_get_history", new_callable=AsyncMock, return_value=[])
    def test_item_history_empty(self, mock_history):
        async def _test():
            cog = _make_cog()
            modal = ItemHistoryModal(cog)
            modal.item_id_input = MagicMock(value="nope")
            inter = _make_interaction()
            await modal.on_submit(inter)
            assert "No history" in inter.followup.send.call_args[0][0]
        _run(_test())


class TestLOAModal:
    @patch("NightCityBot.cogs.fixer_hub._resolve_member", new_callable=AsyncMock)
    def test_start_loa(self, mock_resolve):
        async def _test():
            cog = _make_cog()
            loa_role = MagicMock()
            loa_role.id = 9999
            loa_cog = MagicMock()
            loa_cog.get_loa_role = MagicMock(return_value=loa_role)
            cog.bot.get_cog = MagicMock(return_value=loa_cog)
            member = _make_member(222, "TestPlayer", roles=[])
            mock_resolve.return_value = member
            modal = LOAModal(cog, action="start")
            modal.player_input = MagicMock(value="222")
            inter = _make_interaction()
            await modal.on_submit(inter)
            member.add_roles.assert_called_once()
            assert "now on LOA" in inter.followup.send.call_args[0][0]
        _run(_test())

    @patch("NightCityBot.cogs.fixer_hub._resolve_member", new_callable=AsyncMock)
    def test_start_loa_already(self, mock_resolve):
        async def _test():
            cog = _make_cog()
            loa_role = MagicMock()
            loa_role.id = 9999
            loa_cog = MagicMock()
            loa_cog.get_loa_role = MagicMock(return_value=loa_role)
            cog.bot.get_cog = MagicMock(return_value=loa_cog)
            role_with_id = MagicMock()
            role_with_id.id = 9999
            member = _make_member(222, "TestPlayer", roles=[role_with_id])
            mock_resolve.return_value = member
            modal = LOAModal(cog, action="start")
            modal.player_input = MagicMock(value="222")
            inter = _make_interaction()
            await modal.on_submit(inter)
            member.add_roles.assert_not_called()
            assert "already on LOA" in inter.followup.send.call_args[0][0]
        _run(_test())

    @patch("NightCityBot.cogs.fixer_hub._resolve_member", new_callable=AsyncMock)
    def test_end_loa(self, mock_resolve):
        async def _test():
            cog = _make_cog()
            loa_role = MagicMock()
            loa_role.id = 9999
            loa_cog = MagicMock()
            loa_cog.get_loa_role = MagicMock(return_value=loa_role)
            cog.bot.get_cog = MagicMock(return_value=loa_cog)
            role_with_id = MagicMock()
            role_with_id.id = 9999
            member = _make_member(222, "TestPlayer", roles=[role_with_id])
            mock_resolve.return_value = member
            modal = LOAModal(cog, action="end")
            modal.player_input = MagicMock(value="222")
            inter = _make_interaction()
            await modal.on_submit(inter)
            member.remove_roles.assert_called_once()
            assert "LOA has ended" in inter.followup.send.call_args[0][0]
        _run(_test())

    @patch("NightCityBot.cogs.fixer_hub._resolve_member", new_callable=AsyncMock)
    def test_end_loa_not_on(self, mock_resolve):
        async def _test():
            cog = _make_cog()
            loa_role = MagicMock()
            loa_role.id = 9999
            loa_cog = MagicMock()
            loa_cog.get_loa_role = MagicMock(return_value=loa_role)
            cog.bot.get_cog = MagicMock(return_value=loa_cog)
            member = _make_member(222, "TestPlayer", roles=[])
            mock_resolve.return_value = member
            modal = LOAModal(cog, action="end")
            modal.player_input = MagicMock(value="222")
            inter = _make_interaction()
            await modal.on_submit(inter)
            member.remove_roles.assert_not_called()
            assert "not currently on LOA" in inter.followup.send.call_args[0][0]
        _run(_test())


class TestStoreModals:
    @patch("NightCityBot.cogs.fixer_hub._resolve_member", new_callable=AsyncMock)
    def test_view_gun_store_empty(self, mock_resolve):
        async def _test():
            cog = _make_cog()
            owner = _make_member(222, "ShopOwner")
            mock_resolve.return_value = owner
            guns_cog = MagicMock()
            guns_cog._load_state = AsyncMock(return_value={"stores": {}})
            guns_cog._store_id = MagicMock(return_value="999:222")
            cog.bot.cogs["GunsShopCog"] = guns_cog
            modal = StoreInvModal(cog, store_type="gun")
            modal.owner_input = MagicMock(value="222")
            inter = _make_interaction()
            await modal.on_submit(inter)
            assert "empty" in inter.followup.send.call_args[0][0].lower()
        _run(_test())

    @patch("NightCityBot.cogs.fixer_hub._resolve_member", new_callable=AsyncMock)
    def test_view_gun_store_with_stock(self, mock_resolve):
        async def _test():
            cog = _make_cog()
            owner = _make_member(222, "ShopOwner")
            mock_resolve.return_value = owner
            guns_cog = MagicMock()
            guns_cog._load_state = AsyncMock(return_value={
                "stores": {"999:222": {"lots": [
                    {"gun_name": "Rifle", "unit_cost": 2000, "qty_remaining": 3, "restriction": "basic"},
                ]}}
            })
            guns_cog._store_id = MagicMock(return_value="999:222")
            cog.bot.cogs["GunsShopCog"] = guns_cog
            modal = StoreInvModal(cog, store_type="gun")
            modal.owner_input = MagicMock(value="222")
            inter = _make_interaction()
            await modal.on_submit(inter)
            embed = inter.followup.send.call_args.kwargs["embed"]
            assert "Rifle" in embed.description
        _run(_test())

    @patch("NightCityBot.cogs.fixer_hub._resolve_member", new_callable=AsyncMock)
    def test_view_cw_store(self, mock_resolve):
        async def _test():
            cog = _make_cog()
            owner = _make_member(222, "Doc")
            mock_resolve.return_value = owner
            cw_cog = MagicMock()
            cw_cog._load_inventory = AsyncMock(return_value=[
                {"name": "Sandevistan", "price_paid": 5000, "purchased_at": "2025-01-01"},
            ])
            cw_cog._grouped_inventory = MagicMock(return_value=[
                {"name": "Sandevistan", "price_paid": 5000, "count": 1, "date": "2025-01-01"},
            ])
            cog.bot.cogs["CyberwareShop"] = cw_cog
            modal = StoreInvModal(cog, store_type="cw")
            modal.owner_input = MagicMock(value="222")
            inter = _make_interaction()
            await modal.on_submit(inter)
            embed = inter.followup.send.call_args.kwargs["embed"]
            assert "Sandevistan" in embed.description
        _run(_test())

    @patch("NightCityBot.cogs.fixer_hub._audit_channel", new_callable=AsyncMock, return_value=None)
    @patch("NightCityBot.cogs.fixer_hub._resolve_member", new_callable=AsyncMock)
    def test_store_add(self, mock_resolve, mock_audit):
        async def _test():
            cog = _make_cog()
            owner = _make_member(222, "ShopOwner")
            mock_resolve.return_value = owner
            guns_cog = MagicMock()
            state = {"stores": {}}
            guns_cog._load_state = AsyncMock(return_value=state)
            guns_cog._save_state = AsyncMock(return_value=True)
            guns_cog._store_id = MagicMock(return_value="999:222")
            guns_cog.lock = asyncio.Lock()
            cog.bot.cogs["GunsShopCog"] = guns_cog
            modal = StoreAddModal(cog)
            modal.owner_input = MagicMock(value="222")
            modal.gun_name_input = MagicMock(value="TestGun")
            modal.qty_input = MagicMock(value="5")
            modal.cost_input = MagicMock(value="1000")
            modal.restriction_input = MagicMock(value="basic")
            inter = _make_interaction()
            await modal.on_submit(inter)
            guns_cog._save_state.assert_called_once()
            assert "TestGun" in inter.followup.send.call_args[0][0]
        _run(_test())

    @patch("NightCityBot.cogs.fixer_hub._audit_channel", new_callable=AsyncMock, return_value=None)
    @patch("NightCityBot.cogs.fixer_hub._resolve_member", new_callable=AsyncMock)
    def test_store_remove(self, mock_resolve, mock_audit):
        async def _test():
            cog = _make_cog()
            owner = _make_member(222, "ShopOwner")
            mock_resolve.return_value = owner
            guns_cog = MagicMock()
            state = {"stores": {"999:222": {"lots": [
                {"lot_id": "lot-1", "gun_name": "Pistol", "qty_remaining": 3},
            ]}}}
            guns_cog._load_state = AsyncMock(return_value=state)
            guns_cog._save_state = AsyncMock(return_value=True)
            guns_cog._store_id = MagicMock(return_value="999:222")
            guns_cog.lock = asyncio.Lock()
            cog.bot.cogs["GunsShopCog"] = guns_cog
            modal = StoreRemoveModal(cog)
            modal.owner_input = MagicMock(value="222")
            modal.lot_id_input = MagicMock(value="lot-1")
            modal.qty_input = MagicMock(value="")
            inter = _make_interaction()
            await modal.on_submit(inter)
            assert "Removed" in inter.followup.send.call_args[0][0]
            assert "Pistol" in inter.followup.send.call_args[0][0]
        _run(_test())


class TestWholesaleModals:
    @patch("NightCityBot.cogs.fixer_hub._audit_channel", new_callable=AsyncMock, return_value=None)
    def test_add_gun_to_wholesale(self, mock_audit):
        async def _test():
            cog = _make_cog()
            guns_cog = MagicMock()
            state = {"wholesale_lots": []}
            guns_cog._load_state = AsyncMock(return_value=state)
            guns_cog._save_state = AsyncMock(return_value=True)
            guns_cog.lock = asyncio.Lock()
            cog.bot.cogs["GunsShopCog"] = guns_cog
            modal = WHAddGunModal(cog)
            modal.gun_name_input = MagicMock(value="TestGun")
            modal.qty_input = MagicMock(value="10")
            modal.cost_input = MagicMock(value="5000")
            modal.restriction_input = MagicMock(value="basic")
            inter = _make_interaction()
            await modal.on_submit(inter)
            guns_cog._save_state.assert_called_once()
            assert len(state["wholesale_lots"]) == 1
            assert "TestGun" in inter.followup.send.call_args[0][0]
        _run(_test())

    @patch("NightCityBot.cogs.fixer_hub._audit_channel", new_callable=AsyncMock, return_value=None)
    def test_add_cw_to_wholesale(self, mock_audit):
        async def _test():
            cog = _make_cog()
            cw_cog = MagicMock()
            state = {"cw_wholesale_lots": []}
            cw_cog._load_state = AsyncMock(return_value=state)
            cw_cog._save_state = AsyncMock(return_value=True)
            cw_cog.lock = asyncio.Lock()
            cog.bot.cogs["CyberwareShop"] = cw_cog
            modal = WHAddCWModal(cog)
            modal.item_name_input = MagicMock(value="Sandevistan")
            modal.qty_input = MagicMock(value="5")
            modal.cost_input = MagicMock(value="8000")
            inter = _make_interaction()
            await modal.on_submit(inter)
            cw_cog._save_state.assert_called_once()
            assert len(state["cw_wholesale_lots"]) == 1
            assert "Sandevistan" in inter.followup.send.call_args[0][0]
        _run(_test())

    @patch("NightCityBot.cogs.fixer_hub._audit_channel", new_callable=AsyncMock, return_value=None)
    def test_remove_gun_lot(self, mock_audit):
        async def _test():
            cog = _make_cog()
            guns_cog = MagicMock()
            state = {"wholesale_lots": [
                {"lot_id": "lot-g1", "gun_name": "Pistol", "qty_available": 5},
            ]}
            guns_cog._load_state = AsyncMock(return_value=state)
            guns_cog._save_state = AsyncMock(return_value=True)
            guns_cog.lock = asyncio.Lock()
            cog.bot.cogs["GunsShopCog"] = guns_cog
            modal = WHRemoveLotModal(cog)
            modal.lot_id_input = MagicMock(value="lot-g1")
            modal.qty_input = MagicMock(value="")
            inter = _make_interaction()
            await modal.on_submit(inter)
            assert len(state["wholesale_lots"]) == 0
            assert "Removed" in inter.followup.send.call_args[0][0]
        _run(_test())

    @patch("NightCityBot.cogs.fixer_hub._audit_channel", new_callable=AsyncMock, return_value=None)
    def test_remove_cw_lot(self, mock_audit):
        async def _test():
            cog = _make_cog()
            cw_cog = MagicMock()
            state = {"cw_wholesale_lots": [
                {"lot_id": "lot-cw1", "item_name": "Kiroshi", "qty_available": 3},
            ]}
            cw_cog._load_state = AsyncMock(return_value=state)
            cw_cog._save_state = AsyncMock(return_value=True)
            cw_cog.lock = asyncio.Lock()
            cog.bot.cogs["CyberwareShop"] = cw_cog
            modal = WHRemoveLotModal(cog)
            modal.lot_id_input = MagicMock(value="lot-cw1")
            modal.qty_input = MagicMock(value="")
            inter = _make_interaction()
            await modal.on_submit(inter)
            assert len(state["cw_wholesale_lots"]) == 0
            assert "Kiroshi" in inter.followup.send.call_args[0][0]
        _run(_test())

    @patch("NightCityBot.cogs.fixer_hub._audit_channel", new_callable=AsyncMock, return_value=None)
    def test_remove_lot_partial(self, mock_audit):
        async def _test():
            cog = _make_cog()
            guns_cog = MagicMock()
            state = {"wholesale_lots": [
                {"lot_id": "lot-g2", "gun_name": "SMG", "qty_available": 10},
            ]}
            guns_cog._load_state = AsyncMock(return_value=state)
            guns_cog._save_state = AsyncMock(return_value=True)
            guns_cog.lock = asyncio.Lock()
            cog.bot.cogs["GunsShopCog"] = guns_cog
            modal = WHRemoveLotModal(cog)
            modal.lot_id_input = MagicMock(value="lot-g2")
            modal.qty_input = MagicMock(value="3")
            inter = _make_interaction()
            await modal.on_submit(inter)
            assert state["wholesale_lots"][0]["qty_available"] == 7
            assert "SMG" in inter.followup.send.call_args[0][0]
        _run(_test())

    def test_remove_lot_not_found(self):
        async def _test():
            cog = _make_cog()
            guns_cog = MagicMock()
            guns_cog._load_state = AsyncMock(return_value={"wholesale_lots": []})
            cog.bot.cogs["GunsShopCog"] = guns_cog
            modal = WHRemoveLotModal(cog)
            modal.lot_id_input = MagicMock(value="nope")
            modal.qty_input = MagicMock(value="")
            inter = _make_interaction()
            await modal.on_submit(inter)
            assert "not found" in inter.followup.send.call_args[0][0]
        _run(_test())

    def test_add_gun_bad_input(self):
        async def _test():
            cog = _make_cog()
            guns_cog = MagicMock()
            cog.bot.cogs["GunsShopCog"] = guns_cog
            modal = WHAddGunModal(cog)
            modal.gun_name_input = MagicMock(value="TestGun")
            modal.qty_input = MagicMock(value="abc")
            modal.cost_input = MagicMock(value="xyz")
            modal.restriction_input = MagicMock(value="basic")
            inter = _make_interaction()
            await modal.on_submit(inter)
            assert "numbers" in inter.followup.send.call_args[0][0].lower()
        _run(_test())


def _make_guns_cog(state_dict):
    guns_cog = MagicMock()
    guns_cog.lock = asyncio.Lock()
    guns_cog._load_state = AsyncMock(return_value=state_dict)
    guns_cog._save_state = AsyncMock()
    return guns_cog


def test_store_remove_negative_qty():
    async def _test():
        cog = _make_cog()
        store_key = "999:111"
        guns_cog = _make_guns_cog({
            "stores": {
                store_key: {
                    "lots": [{"lot_id": "L1", "gun_name": "Pistol", "qty_remaining": 5}]
                }
            }
        })
        guns_cog._store_id = MagicMock(return_value=store_key)
        cog.bot.cogs["GunsShopCog"] = guns_cog
        modal = StoreRemoveModal(cog)
        modal.owner_input = MagicMock(value="111")
        modal.lot_id_input = MagicMock(value="L1")
        modal.qty_input = MagicMock(value="-3")
        inter = _make_interaction()
        owner_member = MagicMock()
        owner_member.id = 111
        owner_member.display_name = "TestOwner"
        inter.guild.get_member = MagicMock(return_value=owner_member)
        await modal.on_submit(inter)
        assert "positive" in inter.followup.send.call_args[0][0].lower()
    _run(_test())


def test_store_remove_zero_qty():
    async def _test():
        cog = _make_cog()
        store_key = "999:111"
        guns_cog = _make_guns_cog({
            "stores": {
                store_key: {
                    "lots": [{"lot_id": "L1", "gun_name": "Pistol", "qty_remaining": 5}]
                }
            }
        })
        guns_cog._store_id = MagicMock(return_value=store_key)
        cog.bot.cogs["GunsShopCog"] = guns_cog
        modal = StoreRemoveModal(cog)
        modal.owner_input = MagicMock(value="111")
        modal.lot_id_input = MagicMock(value="L1")
        modal.qty_input = MagicMock(value="0")
        inter = _make_interaction()
        owner_member = MagicMock()
        owner_member.id = 111
        owner_member.display_name = "TestOwner"
        inter.guild.get_member = MagicMock(return_value=owner_member)
        await modal.on_submit(inter)
        assert "positive" in inter.followup.send.call_args[0][0].lower()
    _run(_test())


def test_wh_remove_negative_qty_gun():
    async def _test():
        cog = _make_cog()
        guns_cog = _make_guns_cog({
            "wholesale_lots": [{"lot_id": "WL1", "gun_name": "Rifle", "qty_available": 10}]
        })
        cog.bot.cogs["GunsShopCog"] = guns_cog
        cog.bot.cogs["CyberwareShop"] = MagicMock()
        modal = WHRemoveLotModal(cog)
        modal.lot_id_input = MagicMock(value="WL1")
        modal.qty_input = MagicMock(value="-5")
        inter = _make_interaction()
        await modal.on_submit(inter)
        assert "positive" in inter.followup.send.call_args[0][0].lower()
    _run(_test())


def test_wh_remove_negative_qty_cw():
    async def _test():
        cog = _make_cog()
        guns_cog = _make_guns_cog({"wholesale_lots": []})
        cog.bot.cogs["GunsShopCog"] = guns_cog
        cw_cog = MagicMock()
        cw_cog.lock = asyncio.Lock()
        cw_state = {"cw_wholesale_lots": [{"lot_id": "CW1", "item_name": "Implant", "qty_available": 8}]}
        cw_cog._load_state = AsyncMock(return_value=cw_state)
        cw_cog._save_state = AsyncMock()
        cog.bot.cogs["CyberwareShop"] = cw_cog
        modal = WHRemoveLotModal(cog)
        modal.lot_id_input = MagicMock(value="CW1")
        modal.qty_input = MagicMock(value="-2")
        inter = _make_interaction()
        await modal.on_submit(inter)
        assert "positive" in inter.followup.send.call_args[0][0].lower()
    _run(_test())
