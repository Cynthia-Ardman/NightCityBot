"""Tests for the !player interactive hub."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from NightCityBot.cogs.player_hub import (
    PlayerHubCog,
    PlayerHubView,
    TradeModal,
    TradeConfirmView,
    GiveModal,
)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _find_button(view, label):
    for child in view.children:
        if isinstance(child, discord.ui.Button) and child.label == label:
            return child
    raise ValueError(f"No button with label {label!r}")


def _make_cog():
    bot = MagicMock()
    bot.cogs = {}
    cog = PlayerHubCog(bot)
    return cog


def _make_ctx(author_id=100, guild_id=999):
    ctx = MagicMock()
    ctx.guild = MagicMock()
    ctx.guild.id = guild_id
    ctx.author = MagicMock()
    ctx.author.id = author_id
    ctx.author.display_name = "TestPlayer"
    ctx.send = AsyncMock()
    return ctx


def _make_interaction(user_id=100, guild_id=999):
    inter = MagicMock()
    inter.response = MagicMock()
    inter.response.defer = AsyncMock()
    inter.response.send_modal = AsyncMock()
    inter.response.edit_message = AsyncMock()
    inter.followup = MagicMock()
    inter.followup.send = AsyncMock()
    inter.guild = MagicMock()
    inter.guild.id = guild_id
    inter.guild.get_member = MagicMock(return_value=None)
    inter.user = MagicMock()
    inter.user.id = user_id
    inter.user.display_name = "TestPlayer"
    inter.user.mention = f"<@{user_id}>"
    return inter


def _make_inv_cog(items=None):
    from NightCityBot.cogs.player_inventory import PlayerInventoryCog
    inv_cog = MagicMock(spec=PlayerInventoryCog)
    inv_cog._build_display = PlayerInventoryCog._build_display
    inv_cog.unbelievaboat = MagicMock()
    return inv_cog


# --- Command tests ---

def test_player_cmd_sends_hub():
    async def _test():
        cog = _make_cog()
        ctx = _make_ctx()
        await PlayerHubCog.player_cmd(cog, ctx)
        ctx.send.assert_called_once()
        call_kwargs = ctx.send.call_args
        assert "embed" in call_kwargs.kwargs or len(call_kwargs.args) > 0
        assert "view" in call_kwargs.kwargs
    _run(_test())


def test_player_cmd_no_guild():
    async def _test():
        cog = _make_cog()
        ctx = _make_ctx()
        ctx.guild = None
        await PlayerHubCog.player_cmd(cog, ctx)
        assert "server" in ctx.send.call_args[0][0].lower()
    _run(_test())


def test_helpplayer():
    async def _test():
        cog = _make_cog()
        ctx = _make_ctx()
        await PlayerHubCog.helpplayer(cog, ctx)
        ctx.send.assert_called_once()
    _run(_test())


# --- View tests ---

def test_hub_view_interaction_check():
    async def _test():
        cog = _make_cog()
        ctx = _make_ctx(author_id=100)
        view = PlayerHubView(cog, ctx)
        inter_ok = _make_interaction(user_id=100)
        inter_bad = _make_interaction(user_id=999)
        assert await view.interaction_check(inter_ok) is True
        assert await view.interaction_check(inter_bad) is False
    _run(_test())


def test_view_inv_empty():
    async def _test():
        cog = _make_cog()
        ctx = _make_ctx()
        view = PlayerHubView(cog, ctx)
        inter = _make_interaction()
        btn = _find_button(view, "View Inventory")
        with patch("NightCityBot.cogs.player_hub.pi_get_by_owner", new_callable=AsyncMock, return_value=[]):
            await btn.callback(inter)
        assert "empty" in inter.followup.send.call_args[0][0].lower()
    _run(_test())


def test_view_inv_system_disabled():
    async def _test():
        cog = _make_cog()
        control = MagicMock()
        control.is_enabled = MagicMock(return_value=False)
        cog.bot.cogs["SystemControl"] = control
        cog.bot.get_cog = MagicMock(return_value=control)
        ctx = _make_ctx()
        view = PlayerHubView(cog, ctx)
        inter = _make_interaction()
        btn = _find_button(view, "View Inventory")
        await btn.callback(inter)
        assert "offline" in inter.followup.send.call_args[0][0].lower()
    _run(_test())


def test_view_inv_shows_items():
    async def _test():
        cog = _make_cog()
        inv_cog = _make_inv_cog()
        cog.bot.cogs["PlayerInventory"] = inv_cog
        cog.bot.get_cog = MagicMock(return_value=None)
        items = [
            {
                "item_id": "uuid-1",
                "owner_id": "100",
                "character_name": "V",
                "name": "Katana",
                "item_type": "gun",
                "price_paid": 500,
                "seller_name": "Vendor",
                "acquired_at": "2025-01-01",
                "created_at": "2025-01-01",
            }
        ]
        ctx = _make_ctx()
        view = PlayerHubView(cog, ctx)
        inter = _make_interaction()
        btn = _find_button(view, "View Inventory")
        with patch("NightCityBot.cogs.player_hub.pi_get_by_owner", new_callable=AsyncMock, return_value=items):
            await btn.callback(inter)
        call_kwargs = inter.followup.send.call_args.kwargs
        assert "embed" in call_kwargs
        assert "Katana" in call_kwargs["embed"].description
    _run(_test())


def test_trade_button_opens_modal():
    async def _test():
        cog = _make_cog()
        ctx = _make_ctx()
        view = PlayerHubView(cog, ctx)
        inter = _make_interaction()
        btn = _find_button(view, "Trade Item")
        await btn.callback(inter)
        inter.response.send_modal.assert_called_once()
        modal = inter.response.send_modal.call_args[0][0]
        assert isinstance(modal, TradeModal)
    _run(_test())


def test_give_button_opens_modal():
    async def _test():
        cog = _make_cog()
        ctx = _make_ctx()
        view = PlayerHubView(cog, ctx)
        inter = _make_interaction()
        btn = _find_button(view, "Give Item")
        await btn.callback(inter)
        inter.response.send_modal.assert_called_once()
        modal = inter.response.send_modal.call_args[0][0]
        assert isinstance(modal, GiveModal)
    _run(_test())


# --- Trade Modal tests ---

def test_trade_modal_no_guild():
    async def _test():
        cog = _make_cog()
        modal = TradeModal(cog)
        modal.buyer_input = MagicMock(value="111")
        modal.row_input = MagicMock(value="1")
        modal.price_input = MagicMock(value="100")
        modal.buyer_char_input = MagicMock(value="Johnny")
        inter = _make_interaction()
        inter.guild = None
        await modal.on_submit(inter)
        assert "server" in inter.followup.send.call_args[0][0].lower()
    _run(_test())


def test_trade_modal_bad_buyer():
    async def _test():
        cog = _make_cog()
        cog.bot.get_cog = MagicMock(return_value=None)
        modal = TradeModal(cog)
        modal.buyer_input = MagicMock(value="notanumber")
        modal.row_input = MagicMock(value="1")
        modal.price_input = MagicMock(value="100")
        modal.buyer_char_input = MagicMock(value="Johnny")
        inter = _make_interaction()
        await modal.on_submit(inter)
        assert "could not find" in inter.followup.send.call_args[0][0].lower()
    _run(_test())


def test_trade_modal_bad_row():
    async def _test():
        cog = _make_cog()
        cog.bot.get_cog = MagicMock(return_value=None)
        modal = TradeModal(cog)
        modal.buyer_input = MagicMock(value="111")
        modal.row_input = MagicMock(value="abc")
        modal.price_input = MagicMock(value="100")
        modal.buyer_char_input = MagicMock(value="Johnny")
        inter = _make_interaction()
        buyer = MagicMock()
        buyer.id = 111
        inter.guild.get_member = MagicMock(return_value=buyer)
        await modal.on_submit(inter)
        assert "number" in inter.followup.send.call_args[0][0].lower()
    _run(_test())


def test_trade_modal_negative_price():
    async def _test():
        cog = _make_cog()
        cog.bot.get_cog = MagicMock(return_value=None)
        modal = TradeModal(cog)
        modal.buyer_input = MagicMock(value="111")
        modal.row_input = MagicMock(value="1")
        modal.price_input = MagicMock(value="-50")
        modal.buyer_char_input = MagicMock(value="Johnny")
        inter = _make_interaction()
        buyer = MagicMock()
        buyer.id = 111
        inter.guild.get_member = MagicMock(return_value=buyer)
        await modal.on_submit(inter)
        assert "negative" in inter.followup.send.call_args[0][0].lower()
    _run(_test())


def test_trade_modal_self_trade_nonzero_price():
    async def _test():
        cog = _make_cog()
        cog.bot.get_cog = MagicMock(return_value=None)
        modal = TradeModal(cog)
        modal.buyer_input = MagicMock(value="100")
        modal.row_input = MagicMock(value="1")
        modal.price_input = MagicMock(value="500")
        modal.buyer_char_input = MagicMock(value="Johnny")
        inter = _make_interaction(user_id=100)
        buyer = MagicMock()
        buyer.id = 100
        inter.guild.get_member = MagicMock(return_value=buyer)
        await modal.on_submit(inter)
        assert "self-trade" in inter.followup.send.call_args[0][0].lower()
    _run(_test())


def test_trade_modal_invalid_row_number():
    async def _test():
        cog = _make_cog()
        inv_cog = _make_inv_cog()
        cog.bot.cogs["PlayerInventory"] = inv_cog
        cog.bot.get_cog = MagicMock(return_value=None)
        modal = TradeModal(cog)
        modal.buyer_input = MagicMock(value="111")
        modal.row_input = MagicMock(value="99")
        modal.price_input = MagicMock(value="100")
        modal.buyer_char_input = MagicMock(value="Johnny")
        inter = _make_interaction()
        buyer = MagicMock()
        buyer.id = 111
        inter.guild.get_member = MagicMock(return_value=buyer)
        with patch("NightCityBot.cogs.player_hub.pi_get_by_owner", new_callable=AsyncMock, return_value=[]):
            await modal.on_submit(inter)
        assert "invalid row" in inter.followup.send.call_args[0][0].lower()
    _run(_test())


def test_trade_modal_restricted_item():
    async def _test():
        cog = _make_cog()
        inv_cog = _make_inv_cog()
        cog.bot.cogs["PlayerInventory"] = inv_cog
        cog.bot.get_cog = MagicMock(return_value=None)
        items = [{
            "item_id": "uuid-1", "owner_id": "100", "character_name": "V",
            "name": "Militech Rifle", "item_type": "gun", "restriction": "restricted",
            "price_paid": 5000, "seller_name": "", "acquired_at": "2025-01-01", "created_at": "2025-01-01",
        }]
        modal = TradeModal(cog)
        modal.buyer_input = MagicMock(value="111")
        modal.row_input = MagicMock(value="1")
        modal.price_input = MagicMock(value="100")
        modal.buyer_char_input = MagicMock(value="Johnny")
        inter = _make_interaction(user_id=100)
        buyer = MagicMock()
        buyer.id = 111
        inter.guild.get_member = MagicMock(return_value=buyer)
        with patch("NightCityBot.cogs.player_hub.pi_get_by_owner", new_callable=AsyncMock, return_value=items):
            with patch("NightCityBot.cogs.player_hub.pi_get_item", new_callable=AsyncMock, return_value=items[0]):
                await modal.on_submit(inter)
        assert "restricted" in inter.followup.send.call_args[0][0].lower()
    _run(_test())


def test_trade_modal_self_trade_success():
    async def _test():
        cog = _make_cog()
        inv_cog = _make_inv_cog()
        cog.bot.cogs["PlayerInventory"] = inv_cog
        cog.bot.get_cog = MagicMock(return_value=None)
        items = [{
            "item_id": "uuid-1", "owner_id": "100", "character_name": "V",
            "name": "Katana", "item_type": "gun", "restriction": "basic",
            "price_paid": 500, "seller_name": "", "acquired_at": "2025-01-01", "created_at": "2025-01-01",
        }]
        modal = TradeModal(cog)
        modal.buyer_input = MagicMock(value="100")
        modal.row_input = MagicMock(value="1")
        modal.price_input = MagicMock(value="0")
        modal.buyer_char_input = MagicMock(value="Jackie")
        inter = _make_interaction(user_id=100)
        buyer = MagicMock()
        buyer.id = 100
        buyer.display_name = "TestPlayer"
        buyer.mention = "<@100>"
        inter.guild.get_member = MagicMock(return_value=buyer)
        with patch("NightCityBot.cogs.player_hub.pi_get_by_owner", new_callable=AsyncMock, return_value=items):
            with patch("NightCityBot.cogs.player_hub.pi_get_item", new_callable=AsyncMock, return_value=items[0]):
                with patch("NightCityBot.cogs.player_hub.pi_update_owner", new_callable=AsyncMock, return_value=True):
                    with patch("NightCityBot.cogs.player_hub.ih_record_event", new_callable=AsyncMock):
                        with patch("NightCityBot.cogs.player_hub._route_log_channel", new_callable=AsyncMock, return_value=None):
                            await modal.on_submit(inter)
        assert "traded" in inter.followup.send.call_args[0][0].lower() or "✅" in inter.followup.send.call_args[0][0]
    _run(_test())


# --- Give Modal tests ---

def test_give_modal_no_guild():
    async def _test():
        cog = _make_cog()
        modal = GiveModal(cog)
        modal.target_input = MagicMock(value="111")
        modal.row_input = MagicMock(value="1")
        modal.sender_char_input = MagicMock(value="V")
        modal.receiver_char_input = MagicMock(value="Jackie")
        inter = _make_interaction()
        inter.guild = None
        await modal.on_submit(inter)
        assert "server" in inter.followup.send.call_args[0][0].lower()
    _run(_test())


def test_give_modal_bad_target():
    async def _test():
        cog = _make_cog()
        cog.bot.get_cog = MagicMock(return_value=None)
        modal = GiveModal(cog)
        modal.target_input = MagicMock(value="notanumber")
        modal.row_input = MagicMock(value="1")
        modal.sender_char_input = MagicMock(value="V")
        modal.receiver_char_input = MagicMock(value="Jackie")
        inter = _make_interaction()
        await modal.on_submit(inter)
        assert "could not find" in inter.followup.send.call_args[0][0].lower()
    _run(_test())


def test_give_modal_bad_row():
    async def _test():
        cog = _make_cog()
        cog.bot.get_cog = MagicMock(return_value=None)
        modal = GiveModal(cog)
        modal.target_input = MagicMock(value="111")
        modal.row_input = MagicMock(value="abc")
        modal.sender_char_input = MagicMock(value="V")
        modal.receiver_char_input = MagicMock(value="Jackie")
        inter = _make_interaction()
        target = MagicMock()
        target.id = 111
        inter.guild.get_member = MagicMock(return_value=target)
        await modal.on_submit(inter)
        assert "number" in inter.followup.send.call_args[0][0].lower()
    _run(_test())


def test_give_modal_no_sender_char():
    async def _test():
        cog = _make_cog()
        cog.bot.get_cog = MagicMock(return_value=None)
        modal = GiveModal(cog)
        modal.target_input = MagicMock(value="111")
        modal.row_input = MagicMock(value="1")
        modal.sender_char_input = MagicMock(value="")
        modal.receiver_char_input = MagicMock(value="Jackie")
        inter = _make_interaction()
        target = MagicMock()
        target.id = 111
        inter.guild.get_member = MagicMock(return_value=target)
        await modal.on_submit(inter)
        assert "character name is required" in inter.followup.send.call_args[0][0].lower()
    _run(_test())


def test_give_modal_invalid_row():
    async def _test():
        cog = _make_cog()
        inv_cog = _make_inv_cog()
        cog.bot.cogs["PlayerInventory"] = inv_cog
        cog.bot.get_cog = MagicMock(return_value=None)
        modal = GiveModal(cog)
        modal.target_input = MagicMock(value="111")
        modal.row_input = MagicMock(value="99")
        modal.sender_char_input = MagicMock(value="V")
        modal.receiver_char_input = MagicMock(value="Jackie")
        inter = _make_interaction()
        target = MagicMock()
        target.id = 111
        inter.guild.get_member = MagicMock(return_value=target)
        with patch("NightCityBot.cogs.player_hub.pi_get_by_owner", new_callable=AsyncMock, return_value=[]):
            await modal.on_submit(inter)
        assert "invalid row" in inter.followup.send.call_args[0][0].lower()
    _run(_test())


def test_give_modal_wrong_character():
    async def _test():
        cog = _make_cog()
        inv_cog = _make_inv_cog()
        cog.bot.cogs["PlayerInventory"] = inv_cog
        cog.bot.get_cog = MagicMock(return_value=None)
        items = [{
            "item_id": "uuid-1", "owner_id": "100", "character_name": "V",
            "name": "Katana", "item_type": "gun", "restriction": "basic",
            "price_paid": 500, "seller_name": "", "acquired_at": "2025-01-01", "created_at": "2025-01-01",
        }]
        modal = GiveModal(cog)
        modal.target_input = MagicMock(value="111")
        modal.row_input = MagicMock(value="1")
        modal.sender_char_input = MagicMock(value="WrongName")
        modal.receiver_char_input = MagicMock(value="Jackie")
        inter = _make_interaction(user_id=100)
        target = MagicMock()
        target.id = 111
        inter.guild.get_member = MagicMock(return_value=target)
        with patch("NightCityBot.cogs.player_hub.pi_get_by_owner", new_callable=AsyncMock, return_value=items):
            await modal.on_submit(inter)
        assert "belongs to character" in inter.followup.send.call_args[0][0].lower()
    _run(_test())


def test_give_modal_no_receiver_char():
    async def _test():
        cog = _make_cog()
        inv_cog = _make_inv_cog()
        cog.bot.cogs["PlayerInventory"] = inv_cog
        cog.bot.get_cog = MagicMock(return_value=None)
        items = [{
            "item_id": "uuid-1", "owner_id": "100", "character_name": "V",
            "name": "Katana", "item_type": "gun", "restriction": "basic",
            "price_paid": 500, "seller_name": "", "acquired_at": "2025-01-01", "created_at": "2025-01-01",
        }]
        modal = GiveModal(cog)
        modal.target_input = MagicMock(value="111")
        modal.row_input = MagicMock(value="1")
        modal.sender_char_input = MagicMock(value="V")
        modal.receiver_char_input = MagicMock(value="")
        inter = _make_interaction(user_id=100)
        target = MagicMock()
        target.id = 111
        target.roles = []
        inter.guild.get_member = MagicMock(return_value=target)
        with patch("NightCityBot.cogs.player_hub.pi_get_by_owner", new_callable=AsyncMock, return_value=items):
            await modal.on_submit(inter)
        assert "character name is required" in inter.followup.send.call_args[0][0].lower()
    _run(_test())


def test_give_modal_success():
    async def _test():
        cog = _make_cog()
        inv_cog = _make_inv_cog()
        cog.bot.cogs["PlayerInventory"] = inv_cog
        cog.bot.get_cog = MagicMock(return_value=None)
        items = [{
            "item_id": "uuid-1", "owner_id": "100", "character_name": "V",
            "name": "Katana", "item_type": "gun", "restriction": "basic",
            "price_paid": 500, "seller_name": "", "acquired_at": "2025-01-01", "created_at": "2025-01-01",
        }]
        modal = GiveModal(cog)
        modal.target_input = MagicMock(value="111")
        modal.row_input = MagicMock(value="1")
        modal.sender_char_input = MagicMock(value="V")
        modal.receiver_char_input = MagicMock(value="Jackie")
        inter = _make_interaction(user_id=100)
        target = MagicMock()
        target.id = 111
        target.display_name = "OtherPlayer"
        target.mention = "<@111>"
        target.roles = []
        inter.guild.get_member = MagicMock(return_value=target)
        with patch("NightCityBot.cogs.player_hub.pi_get_by_owner", new_callable=AsyncMock, return_value=items):
            with patch("NightCityBot.cogs.player_hub.pi_update_owner", new_callable=AsyncMock, return_value=True):
                with patch("NightCityBot.cogs.player_hub.ih_record_event", new_callable=AsyncMock):
                    with patch("NightCityBot.cogs.player_hub._route_log_channel", new_callable=AsyncMock, return_value=None):
                        await modal.on_submit(inter)
        assert "transferred" in inter.followup.send.call_args[0][0].lower() or "✅" in inter.followup.send.call_args[0][0]
    _run(_test())


def test_trade_modal_system_disabled():
    async def _test():
        cog = _make_cog()
        control = MagicMock()
        control.is_enabled = MagicMock(return_value=False)
        cog.bot.get_cog = MagicMock(return_value=control)
        modal = TradeModal(cog)
        modal.buyer_input = MagicMock(value="111")
        modal.row_input = MagicMock(value="1")
        modal.price_input = MagicMock(value="100")
        modal.buyer_char_input = MagicMock(value="Johnny")
        inter = _make_interaction()
        buyer = MagicMock()
        buyer.id = 111
        inter.guild.get_member = MagicMock(return_value=buyer)
        await modal.on_submit(inter)
        assert "offline" in inter.followup.send.call_args[0][0].lower()
    _run(_test())


def test_give_modal_system_disabled():
    async def _test():
        cog = _make_cog()
        control = MagicMock()
        control.is_enabled = MagicMock(return_value=False)
        cog.bot.get_cog = MagicMock(return_value=control)
        modal = GiveModal(cog)
        modal.target_input = MagicMock(value="111")
        modal.row_input = MagicMock(value="1")
        modal.sender_char_input = MagicMock(value="V")
        modal.receiver_char_input = MagicMock(value="Jackie")
        inter = _make_interaction()
        target = MagicMock()
        target.id = 111
        inter.guild.get_member = MagicMock(return_value=target)
        await modal.on_submit(inter)
        assert "offline" in inter.followup.send.call_args[0][0].lower()
    _run(_test())


def test_trade_modal_empty_buyer_char():
    async def _test():
        cog = _make_cog()
        cog.bot.get_cog = MagicMock(return_value=None)
        modal = TradeModal(cog)
        modal.buyer_input = MagicMock(value="111")
        modal.row_input = MagicMock(value="1")
        modal.price_input = MagicMock(value="100")
        modal.buyer_char_input = MagicMock(value="   ")
        inter = _make_interaction()
        buyer = MagicMock()
        buyer.id = 111
        inter.guild.get_member = MagicMock(return_value=buyer)
        await modal.on_submit(inter)
        assert "buyer character name is required" in inter.followup.send.call_args[0][0].lower()
    _run(_test())


def test_trade_modal_bad_price():
    async def _test():
        cog = _make_cog()
        cog.bot.get_cog = MagicMock(return_value=None)
        modal = TradeModal(cog)
        modal.buyer_input = MagicMock(value="111")
        modal.row_input = MagicMock(value="1")
        modal.price_input = MagicMock(value="abc")
        modal.buyer_char_input = MagicMock(value="Johnny")
        inter = _make_interaction()
        buyer = MagicMock()
        buyer.id = 111
        inter.guild.get_member = MagicMock(return_value=buyer)
        await modal.on_submit(inter)
        assert "price must be a number" in inter.followup.send.call_args[0][0].lower()
    _run(_test())


def test_trade_confirm_view_accept():
    async def _test():
        view = TradeConfirmView(timeout=5)
        inter = _make_interaction()
        btn = _find_button(view, "Accept")
        await btn.callback(inter)
        assert view.accepted is True
    _run(_test())


def test_trade_confirm_view_decline():
    async def _test():
        view = TradeConfirmView(timeout=5)
        inter = _make_interaction()
        btn = _find_button(view, "Decline")
        await btn.callback(inter)
        assert view.accepted is False
    _run(_test())
