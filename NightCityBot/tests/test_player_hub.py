"""Tests for the !player interactive hub."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from NightCityBot.cogs.player_hub import (
    PlayerHubCog,
    PlayerHubView,
    TradeSetupView,
    TradeDetailsModal,
    TradeConfirmView,
    GiveSetupView,
    GiveDetailsModal,
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
    inter.response.send_message = AsyncMock()
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


SAMPLE_ITEMS = [{
    "item_id": "uuid-1", "owner_id": "100", "character_name": "V",
    "name": "Katana", "item_type": "gun", "restriction": "basic",
    "price_paid": 500, "seller_name": "", "acquired_at": "2025-01-01",
    "created_at": "2025-01-01",
}]

RESTRICTED_ITEMS = [{
    "item_id": "uuid-1", "owner_id": "100", "character_name": "V",
    "name": "Militech Rifle", "item_type": "gun", "restriction": "restricted",
    "price_paid": 5000, "seller_name": "", "acquired_at": "2025-01-01",
    "created_at": "2025-01-01",
}]


def _build_groups(items):
    from NightCityBot.cogs.player_inventory import PlayerInventoryCog
    _, groups = PlayerInventoryCog._build_display(items)
    return groups


def _make_buyer(uid=111, name="BuyerPlayer"):
    b = MagicMock(spec=discord.Member)
    b.id = uid
    b.display_name = name
    b.mention = f"<@{uid}>"
    b.roles = []
    return b


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
        ctx = _make_ctx()
        view = PlayerHubView(cog, ctx)
        inter = _make_interaction()
        btn = _find_button(view, "View Inventory")
        with patch("NightCityBot.cogs.player_hub.pi_get_by_owner", new_callable=AsyncMock, return_value=SAMPLE_ITEMS):
            await btn.callback(inter)
        call_kwargs = inter.followup.send.call_args.kwargs
        assert "embed" in call_kwargs
        assert "Katana" in call_kwargs["embed"].description
    _run(_test())


def test_trade_button_opens_setup():
    async def _test():
        cog = _make_cog()
        inv_cog = _make_inv_cog()
        cog.bot.cogs["PlayerInventory"] = inv_cog
        cog.bot.get_cog = MagicMock(return_value=None)
        ctx = _make_ctx()
        view = PlayerHubView(cog, ctx)
        inter = _make_interaction()
        btn = _find_button(view, "Trade Item")
        with patch("NightCityBot.cogs.player_hub.pi_get_by_owner", new_callable=AsyncMock, return_value=SAMPLE_ITEMS):
            await btn.callback(inter)
        call_kwargs = inter.followup.send.call_args.kwargs
        assert "view" in call_kwargs
        assert isinstance(call_kwargs["view"], TradeSetupView)
    _run(_test())


def test_trade_button_empty_inv():
    async def _test():
        cog = _make_cog()
        inv_cog = _make_inv_cog()
        cog.bot.cogs["PlayerInventory"] = inv_cog
        cog.bot.get_cog = MagicMock(return_value=None)
        ctx = _make_ctx()
        view = PlayerHubView(cog, ctx)
        inter = _make_interaction()
        btn = _find_button(view, "Trade Item")
        with patch("NightCityBot.cogs.player_hub.pi_get_by_owner", new_callable=AsyncMock, return_value=[]):
            await btn.callback(inter)
        assert "empty" in inter.followup.send.call_args[0][0].lower()
    _run(_test())


def test_give_button_opens_setup():
    async def _test():
        cog = _make_cog()
        inv_cog = _make_inv_cog()
        cog.bot.cogs["PlayerInventory"] = inv_cog
        cog.bot.get_cog = MagicMock(return_value=None)
        ctx = _make_ctx()
        view = PlayerHubView(cog, ctx)
        inter = _make_interaction()
        btn = _find_button(view, "Give Item")
        with patch("NightCityBot.cogs.player_hub.pi_get_by_owner", new_callable=AsyncMock, return_value=SAMPLE_ITEMS):
            await btn.callback(inter)
        call_kwargs = inter.followup.send.call_args.kwargs
        assert "view" in call_kwargs
        assert isinstance(call_kwargs["view"], GiveSetupView)
    _run(_test())


def test_give_button_empty_inv():
    async def _test():
        cog = _make_cog()
        inv_cog = _make_inv_cog()
        cog.bot.cogs["PlayerInventory"] = inv_cog
        cog.bot.get_cog = MagicMock(return_value=None)
        ctx = _make_ctx()
        view = PlayerHubView(cog, ctx)
        inter = _make_interaction()
        btn = _find_button(view, "Give Item")
        with patch("NightCityBot.cogs.player_hub.pi_get_by_owner", new_callable=AsyncMock, return_value=[]):
            await btn.callback(inter)
        assert "empty" in inter.followup.send.call_args[0][0].lower()
    _run(_test())


# --- Trade Setup View tests ---

def test_trade_setup_interaction_check():
    async def _test():
        cog = _make_cog()
        ctx = _make_ctx(author_id=100)
        groups = _build_groups(SAMPLE_ITEMS)
        view = TradeSetupView(cog, ctx, groups)
        inter_ok = _make_interaction(user_id=100)
        inter_bad = _make_interaction(user_id=999)
        assert await view.interaction_check(inter_ok) is True
        assert await view.interaction_check(inter_bad) is False
    _run(_test())


def _find_select(view, placeholder_substring):
    for child in view.children:
        if isinstance(child, discord.ui.UserSelect):
            if placeholder_substring.lower() in (child.placeholder or "").lower():
                return child
        if isinstance(child, discord.ui.Select):
            if placeholder_substring.lower() in (child.placeholder or "").lower():
                return child
    raise ValueError(f"No select with placeholder containing {placeholder_substring!r}")


def _find_any_select(view, cls=discord.ui.UserSelect):
    for child in view.children:
        if isinstance(child, cls):
            return child
    raise ValueError(f"No select of type {cls.__name__}")


def test_trade_setup_buyer_select():
    async def _test():
        cog = _make_cog()
        ctx = _make_ctx()
        groups = _build_groups(SAMPLE_ITEMS)
        view = TradeSetupView(cog, ctx, groups)
        inter = _make_interaction()
        buyer = _make_buyer()
        select = _find_any_select(view, discord.ui.UserSelect)
        select._values = [buyer]
        await select.callback(inter)
        assert view.selected_buyer == buyer
        assert "✓" in inter.response.send_message.call_args[0][0]
    _run(_test())


def test_trade_setup_item_select():
    async def _test():
        cog = _make_cog()
        ctx = _make_ctx()
        groups = _build_groups(SAMPLE_ITEMS)
        view = TradeSetupView(cog, ctx, groups)
        inter = _make_interaction()
        inter.data = {"values": ["0"]}
        await view._on_item_select(inter)
        assert view.selected_group_idx == 0
        assert "Katana" in inter.response.send_message.call_args[0][0]
    _run(_test())


def test_trade_setup_continue_no_buyer():
    async def _test():
        cog = _make_cog()
        ctx = _make_ctx()
        groups = _build_groups(SAMPLE_ITEMS)
        view = TradeSetupView(cog, ctx, groups)
        view.selected_group_idx = 0
        inter = _make_interaction()
        btn = _find_button(view, "Continue →")
        await btn.callback(inter)
        assert "buyer" in inter.response.send_message.call_args[0][0].lower()
    _run(_test())


def test_trade_setup_continue_no_item():
    async def _test():
        cog = _make_cog()
        ctx = _make_ctx()
        groups = _build_groups(SAMPLE_ITEMS)
        view = TradeSetupView(cog, ctx, groups)
        view.selected_buyer = _make_buyer()
        inter = _make_interaction()
        btn = _find_button(view, "Continue →")
        await btn.callback(inter)
        assert "item" in inter.response.send_message.call_args[0][0].lower()
    _run(_test())


def test_trade_setup_continue_opens_modal():
    async def _test():
        cog = _make_cog()
        ctx = _make_ctx()
        groups = _build_groups(SAMPLE_ITEMS)
        view = TradeSetupView(cog, ctx, groups)
        view.selected_buyer = _make_buyer()
        view.selected_group_idx = 0
        inter = _make_interaction()
        btn = _find_button(view, "Continue →")
        await btn.callback(inter)
        inter.response.send_modal.assert_called_once()
        modal = inter.response.send_modal.call_args[0][0]
        assert isinstance(modal, TradeDetailsModal)
    _run(_test())


def test_trade_setup_continue_self_trade_blocked():
    async def _test():
        cog = _make_cog()
        ctx = _make_ctx()
        groups = _build_groups(SAMPLE_ITEMS)
        view = TradeSetupView(cog, ctx, groups)
        view.selected_buyer = _make_buyer(uid=100)
        view.selected_group_idx = 0
        inter = _make_interaction(user_id=100)
        btn = _find_button(view, "Continue →")
        await btn.callback(inter)
        assert "cannot trade items to yourself" in inter.response.send_message.call_args[0][0].lower()
        inter.response.send_modal.assert_not_called()
    _run(_test())


def test_trade_setup_continue_restricted_blocked():
    async def _test():
        cog = _make_cog()
        ctx = _make_ctx()
        groups = _build_groups(RESTRICTED_ITEMS)
        view = TradeSetupView(cog, ctx, groups)
        view.selected_buyer = _make_buyer()
        view.selected_group_idx = 0
        inter = _make_interaction()
        btn = _find_button(view, "Continue →")
        await btn.callback(inter)
        assert "restricted" in inter.response.send_message.call_args[0][0].lower()
        inter.response.send_modal.assert_not_called()
    _run(_test())


# --- Trade Details Modal tests ---

def test_trade_details_no_guild():
    async def _test():
        cog = _make_cog()
        buyer = _make_buyer()
        groups = _build_groups(SAMPLE_ITEMS)
        modal = TradeDetailsModal(cog, buyer, groups[0])
        modal.price_input = MagicMock(value="100")
        modal.buyer_char_input = MagicMock(value="Johnny")
        inter = _make_interaction()
        inter.guild = None
        await modal.on_submit(inter)
        assert "server" in inter.followup.send.call_args[0][0].lower()
    _run(_test())


def test_trade_details_system_disabled():
    async def _test():
        cog = _make_cog()
        control = MagicMock()
        control.is_enabled = MagicMock(return_value=False)
        cog.bot.get_cog = MagicMock(return_value=control)
        buyer = _make_buyer()
        groups = _build_groups(SAMPLE_ITEMS)
        modal = TradeDetailsModal(cog, buyer, groups[0])
        modal.price_input = MagicMock(value="100")
        modal.buyer_char_input = MagicMock(value="Johnny")
        inter = _make_interaction()
        await modal.on_submit(inter)
        assert "offline" in inter.followup.send.call_args[0][0].lower()
    _run(_test())


def test_trade_details_negative_price():
    async def _test():
        cog = _make_cog()
        cog.bot.get_cog = MagicMock(return_value=None)
        buyer = _make_buyer()
        groups = _build_groups(SAMPLE_ITEMS)
        modal = TradeDetailsModal(cog, buyer, groups[0])
        modal.price_input = MagicMock(value="-50")
        modal.buyer_char_input = MagicMock(value="Johnny")
        inter = _make_interaction()
        await modal.on_submit(inter)
        assert "negative" in inter.followup.send.call_args[0][0].lower()
    _run(_test())


def test_trade_details_bad_price():
    async def _test():
        cog = _make_cog()
        cog.bot.get_cog = MagicMock(return_value=None)
        buyer = _make_buyer()
        groups = _build_groups(SAMPLE_ITEMS)
        modal = TradeDetailsModal(cog, buyer, groups[0])
        modal.price_input = MagicMock(value="abc")
        modal.buyer_char_input = MagicMock(value="Johnny")
        inter = _make_interaction()
        await modal.on_submit(inter)
        assert "price must be a number" in inter.followup.send.call_args[0][0].lower()
    _run(_test())


def test_trade_details_self_trade_blocked():
    async def _test():
        cog = _make_cog()
        cog.bot.get_cog = MagicMock(return_value=None)
        buyer = _make_buyer(uid=100)
        groups = _build_groups(SAMPLE_ITEMS)
        modal = TradeDetailsModal(cog, buyer, groups[0])
        modal.price_input = MagicMock(value="0")
        modal.buyer_char_input = MagicMock(value="Johnny")
        inter = _make_interaction(user_id=100)
        await modal.on_submit(inter)
        assert "cannot trade items to yourself" in inter.followup.send.call_args[0][0].lower()
    _run(_test())


def test_trade_details_empty_buyer_char():
    async def _test():
        cog = _make_cog()
        cog.bot.get_cog = MagicMock(return_value=None)
        buyer = _make_buyer()
        groups = _build_groups(SAMPLE_ITEMS)
        modal = TradeDetailsModal(cog, buyer, groups[0])
        modal.price_input = MagicMock(value="100")
        modal.buyer_char_input = MagicMock(value="   ")
        inter = _make_interaction()
        await modal.on_submit(inter)
        assert "buyer character name is required" in inter.followup.send.call_args[0][0].lower()
    _run(_test())


def test_trade_details_restricted_item():
    async def _test():
        cog = _make_cog()
        inv_cog = _make_inv_cog()
        cog.bot.cogs["PlayerInventory"] = inv_cog
        cog.bot.get_cog = MagicMock(return_value=None)
        buyer = _make_buyer()
        groups = _build_groups(RESTRICTED_ITEMS)
        modal = TradeDetailsModal(cog, buyer, groups[0])
        modal.price_input = MagicMock(value="100")
        modal.buyer_char_input = MagicMock(value="Johnny")
        inter = _make_interaction(user_id=100)
        with patch("NightCityBot.cogs.player_hub.pi_get_item", new_callable=AsyncMock, return_value=RESTRICTED_ITEMS[0]):
            await modal.on_submit(inter)
        assert "restricted" in inter.followup.send.call_args[0][0].lower()
    _run(_test())


def test_trade_details_self_trade_blocked_free():
    async def _test():
        cog = _make_cog()
        cog.bot.get_cog = MagicMock(return_value=None)
        buyer = _make_buyer(uid=100, name="TestPlayer")
        groups = _build_groups(SAMPLE_ITEMS)
        modal = TradeDetailsModal(cog, buyer, groups[0])
        modal.price_input = MagicMock(value="0")
        modal.buyer_char_input = MagicMock(value="Jackie")
        inter = _make_interaction(user_id=100)
        await modal.on_submit(inter)
        assert "cannot trade items to yourself" in inter.followup.send.call_args[0][0].lower()
    _run(_test())


def test_trade_details_item_no_longer_owned():
    async def _test():
        cog = _make_cog()
        inv_cog = _make_inv_cog()
        cog.bot.cogs["PlayerInventory"] = inv_cog
        cog.bot.get_cog = MagicMock(return_value=None)
        buyer = _make_buyer()
        groups = _build_groups(SAMPLE_ITEMS)
        modal = TradeDetailsModal(cog, buyer, groups[0])
        modal.price_input = MagicMock(value="100")
        modal.buyer_char_input = MagicMock(value="Johnny")
        inter = _make_interaction(user_id=100)
        with patch("NightCityBot.cogs.player_hub.pi_get_item", new_callable=AsyncMock, return_value=None):
            await modal.on_submit(inter)
        assert "no longer" in inter.followup.send.call_args[0][0].lower()
    _run(_test())


# --- Give Setup View tests ---

def test_give_setup_interaction_check():
    async def _test():
        cog = _make_cog()
        ctx = _make_ctx(author_id=100)
        groups = _build_groups(SAMPLE_ITEMS)
        view = GiveSetupView(cog, ctx, groups)
        inter_ok = _make_interaction(user_id=100)
        inter_bad = _make_interaction(user_id=999)
        assert await view.interaction_check(inter_ok) is True
        assert await view.interaction_check(inter_bad) is False
    _run(_test())


def test_give_setup_recipient_select():
    async def _test():
        cog = _make_cog()
        ctx = _make_ctx()
        groups = _build_groups(SAMPLE_ITEMS)
        view = GiveSetupView(cog, ctx, groups)
        inter = _make_interaction()
        recipient = _make_buyer(uid=222, name="Recipient")
        select = _find_any_select(view, discord.ui.UserSelect)
        select._values = [recipient]
        await select.callback(inter)
        assert view.selected_recipient == recipient
        assert "✓" in inter.response.send_message.call_args[0][0]
    _run(_test())


def test_give_setup_item_select():
    async def _test():
        cog = _make_cog()
        ctx = _make_ctx()
        groups = _build_groups(SAMPLE_ITEMS)
        view = GiveSetupView(cog, ctx, groups)
        inter = _make_interaction()
        inter.data = {"values": ["0"]}
        await view._on_item_select(inter)
        assert view.selected_group_idx == 0
        assert "Katana" in inter.response.send_message.call_args[0][0]
    _run(_test())


def test_give_setup_continue_no_recipient():
    async def _test():
        cog = _make_cog()
        ctx = _make_ctx()
        groups = _build_groups(SAMPLE_ITEMS)
        view = GiveSetupView(cog, ctx, groups)
        view.selected_group_idx = 0
        inter = _make_interaction()
        btn = _find_button(view, "Continue →")
        await btn.callback(inter)
        assert "recipient" in inter.response.send_message.call_args[0][0].lower()
    _run(_test())


def test_give_setup_continue_no_item():
    async def _test():
        cog = _make_cog()
        ctx = _make_ctx()
        groups = _build_groups(SAMPLE_ITEMS)
        view = GiveSetupView(cog, ctx, groups)
        view.selected_recipient = _make_buyer()
        inter = _make_interaction()
        btn = _find_button(view, "Continue →")
        await btn.callback(inter)
        assert "item" in inter.response.send_message.call_args[0][0].lower()
    _run(_test())


def test_give_setup_continue_opens_modal():
    async def _test():
        cog = _make_cog()
        ctx = _make_ctx()
        groups = _build_groups(SAMPLE_ITEMS)
        view = GiveSetupView(cog, ctx, groups)
        view.selected_recipient = _make_buyer()
        view.selected_group_idx = 0
        inter = _make_interaction()
        btn = _find_button(view, "Continue →")
        await btn.callback(inter)
        inter.response.send_modal.assert_called_once()
        modal = inter.response.send_modal.call_args[0][0]
        assert isinstance(modal, GiveDetailsModal)
    _run(_test())


# --- Give Details Modal tests ---

def test_give_details_no_guild():
    async def _test():
        cog = _make_cog()
        recipient = _make_buyer()
        groups = _build_groups(SAMPLE_ITEMS)
        modal = GiveDetailsModal(cog, recipient, groups[0])
        modal.sender_char_input = MagicMock(value="V")
        modal.receiver_char_input = MagicMock(value="Jackie")
        inter = _make_interaction()
        inter.guild = None
        await modal.on_submit(inter)
        assert "server" in inter.followup.send.call_args[0][0].lower()
    _run(_test())


def test_give_details_system_disabled():
    async def _test():
        cog = _make_cog()
        control = MagicMock()
        control.is_enabled = MagicMock(return_value=False)
        cog.bot.get_cog = MagicMock(return_value=control)
        recipient = _make_buyer()
        groups = _build_groups(SAMPLE_ITEMS)
        modal = GiveDetailsModal(cog, recipient, groups[0])
        modal.sender_char_input = MagicMock(value="V")
        modal.receiver_char_input = MagicMock(value="Jackie")
        inter = _make_interaction()
        await modal.on_submit(inter)
        assert "offline" in inter.followup.send.call_args[0][0].lower()
    _run(_test())


def test_give_details_no_sender_char():
    async def _test():
        cog = _make_cog()
        cog.bot.get_cog = MagicMock(return_value=None)
        recipient = _make_buyer()
        groups = _build_groups(SAMPLE_ITEMS)
        modal = GiveDetailsModal(cog, recipient, groups[0])
        modal.sender_char_input = MagicMock(value="")
        modal.receiver_char_input = MagicMock(value="Jackie")
        inter = _make_interaction()
        await modal.on_submit(inter)
        assert "character name is required" in inter.followup.send.call_args[0][0].lower()
    _run(_test())


def test_give_details_wrong_character():
    async def _test():
        cog = _make_cog()
        cog.bot.get_cog = MagicMock(return_value=None)
        recipient = _make_buyer()
        groups = _build_groups(SAMPLE_ITEMS)
        modal = GiveDetailsModal(cog, recipient, groups[0])
        modal.sender_char_input = MagicMock(value="WrongName")
        modal.receiver_char_input = MagicMock(value="Jackie")
        inter = _make_interaction(user_id=100)
        await modal.on_submit(inter)
        assert "belongs to character" in inter.followup.send.call_args[0][0].lower()
    _run(_test())


def test_give_details_no_receiver_char():
    async def _test():
        cog = _make_cog()
        cog.bot.get_cog = MagicMock(return_value=None)
        recipient = _make_buyer()
        recipient.roles = []
        groups = _build_groups(SAMPLE_ITEMS)
        modal = GiveDetailsModal(cog, recipient, groups[0])
        modal.sender_char_input = MagicMock(value="V")
        modal.receiver_char_input = MagicMock(value="")
        inter = _make_interaction(user_id=100)
        await modal.on_submit(inter)
        assert "character name is required" in inter.followup.send.call_args[0][0].lower()
    _run(_test())


def test_give_details_success():
    async def _test():
        cog = _make_cog()
        inv_cog = _make_inv_cog()
        cog.bot.cogs["PlayerInventory"] = inv_cog
        cog.bot.get_cog = MagicMock(return_value=None)
        recipient = _make_buyer()
        recipient.roles = []
        groups = _build_groups(SAMPLE_ITEMS)
        modal = GiveDetailsModal(cog, recipient, groups[0])
        modal.sender_char_input = MagicMock(value="V")
        modal.receiver_char_input = MagicMock(value="Jackie")
        inter = _make_interaction(user_id=100)
        with patch("NightCityBot.cogs.player_hub.pi_update_owner", new_callable=AsyncMock, return_value=True):
            with patch("NightCityBot.cogs.player_hub.ih_record_event", new_callable=AsyncMock):
                with patch("NightCityBot.cogs.player_hub._route_log_channel", new_callable=AsyncMock, return_value=None):
                    await modal.on_submit(inter)
        assert "transferred" in inter.followup.send.call_args[0][0].lower() or "✅" in inter.followup.send.call_args[0][0]
    _run(_test())


def test_give_details_transfer_fails():
    async def _test():
        cog = _make_cog()
        cog.bot.get_cog = MagicMock(return_value=None)
        recipient = _make_buyer()
        recipient.roles = []
        groups = _build_groups(SAMPLE_ITEMS)
        modal = GiveDetailsModal(cog, recipient, groups[0])
        modal.sender_char_input = MagicMock(value="V")
        modal.receiver_char_input = MagicMock(value="Jackie")
        inter = _make_interaction(user_id=100)
        with patch("NightCityBot.cogs.player_hub.pi_update_owner", new_callable=AsyncMock, return_value=False):
            await modal.on_submit(inter)
        assert "failed" in inter.followup.send.call_args[0][0].lower()
    _run(_test())


# --- Trade Confirm View ---

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


# --- Trade button system disabled ---

def test_trade_button_system_disabled():
    async def _test():
        cog = _make_cog()
        control = MagicMock()
        control.is_enabled = MagicMock(return_value=False)
        cog.bot.get_cog = MagicMock(return_value=control)
        ctx = _make_ctx()
        view = PlayerHubView(cog, ctx)
        inter = _make_interaction()
        btn = _find_button(view, "Trade Item")
        await btn.callback(inter)
        assert "offline" in inter.followup.send.call_args[0][0].lower()
    _run(_test())


def test_give_button_system_disabled():
    async def _test():
        cog = _make_cog()
        control = MagicMock()
        control.is_enabled = MagicMock(return_value=False)
        cog.bot.get_cog = MagicMock(return_value=control)
        ctx = _make_ctx()
        view = PlayerHubView(cog, ctx)
        inter = _make_interaction()
        btn = _find_button(view, "Give Item")
        await btn.callback(inter)
        assert "offline" in inter.followup.send.call_args[0][0].lower()
    _run(_test())
