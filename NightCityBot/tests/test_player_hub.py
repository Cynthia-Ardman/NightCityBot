"""Tests for the !player interactive hub."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from NightCityBot.cogs.player_hub import (
    PlayerHubCog,
    PlayerHubView,
    TradeSetupView,
    TradeConfirmView,
    GiveSetupView,
    SellToStoreSetupView,
    StoreBuyConfirmView,
    ManageCharactersView,
    DeactivateCharacterView,
    ReactivateCharacterView,
    InventoryCharFilterView,
    _build_inventory_embed,
    _process_trade,
    _process_give,
    _process_sell_to_store,
)

MOCK_ACTIVE_CHARS = [
    {"character_id": "uuid-char-1", "user_id": "111", "name": "Johnny", "active": True},
    {"character_id": "uuid-char-2", "user_id": "111", "name": "V", "active": True},
]

MOCK_INACTIVE_CHARS = [
    {"character_id": "uuid-char-3", "user_id": "111", "name": "Jackie", "active": False},
]

MOCK_SELLER_CHARS = [
    {"character_id": "uuid-char-10", "user_id": "100", "name": "V", "active": True},
]


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
        with patch("NightCityBot.cogs.player_hub.get_active_characters", new_callable=AsyncMock, return_value=MOCK_ACTIVE_CHARS):
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


@patch("NightCityBot.cogs.player_hub.collect_text_input", new_callable=AsyncMock, return_value=None)
def test_trade_setup_continue_starts_inline_flow(mock_collect):
    async def _test():
        cog = _make_cog()
        ctx = _make_ctx()
        groups = _build_groups(SAMPLE_ITEMS)
        view = TradeSetupView(cog, ctx, groups)
        view.selected_buyer = _make_buyer()
        view.selected_group_idx = 0
        view.selected_buyer_char_name = "Johnny"
        inter = _make_interaction()
        inter.channel_id = 123
        btn = _find_button(view, "Continue →")
        await btn.callback(inter)
        inter.response.defer.assert_called_once()
    _run(_test())


def test_trade_setup_continue_no_buyer_char():
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
        assert "character" in inter.response.send_message.call_args[0][0].lower()
    _run(_test())


def test_trade_setup_continue_self_trade_blocked():
    async def _test():
        cog = _make_cog()
        ctx = _make_ctx()
        groups = _build_groups(SAMPLE_ITEMS)
        view = TradeSetupView(cog, ctx, groups)
        view.selected_buyer = _make_buyer(uid=100)
        view.selected_group_idx = 0
        view.selected_buyer_char_name = "Johnny"
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
        view.selected_buyer_char_name = "Johnny"
        inter = _make_interaction()
        btn = _find_button(view, "Continue →")
        await btn.callback(inter)
        assert "restricted" in inter.response.send_message.call_args[0][0].lower()
        inter.response.send_modal.assert_not_called()
    _run(_test())


# --- Trade Details Modal tests ---

def test_trade_process_no_guild():
    async def _test():
        cog = _make_cog()
        buyer = _make_buyer()
        groups = _build_groups(SAMPLE_ITEMS)
        inter = _make_interaction()
        inter.guild = None
        await _process_trade(cog, inter, buyer, groups[0], "Johnny", 100)
        assert "server" in inter.followup.send.call_args[0][0].lower()
    _run(_test())


def test_trade_process_system_disabled():
    async def _test():
        cog = _make_cog()
        control = MagicMock()
        control.is_enabled = MagicMock(return_value=False)
        cog.bot.get_cog = MagicMock(return_value=control)
        buyer = _make_buyer()
        groups = _build_groups(SAMPLE_ITEMS)
        inter = _make_interaction()
        await _process_trade(cog, inter, buyer, groups[0], "Johnny", 100)
        assert "offline" in inter.followup.send.call_args[0][0].lower()
    _run(_test())


def test_trade_process_zero_price_allowed():
    async def _test():
        cog = _make_cog()
        cog.bot.get_cog = MagicMock(return_value=None)
        buyer = _make_buyer()
        groups = _build_groups(SAMPLE_ITEMS)
        inter = _make_interaction()
        with patch("NightCityBot.cogs.player_hub.pi_get_item", new_callable=AsyncMock, return_value={"owner_id": "100"}):
            with patch("NightCityBot.cogs.player_hub.TradeConfirmView") as MockConfirm:
                confirm_inst = MagicMock()
                confirm_inst.accepted = False
                confirm_inst.wait = AsyncMock()
                MockConfirm.return_value = confirm_inst
                await _process_trade(cog, inter, buyer, groups[0], "Johnny", 0)
        assert inter.followup.send.called
    _run(_test())


def test_trade_process_self_trade_blocked():
    async def _test():
        cog = _make_cog()
        cog.bot.get_cog = MagicMock(return_value=None)
        buyer = _make_buyer(uid=100)
        groups = _build_groups(SAMPLE_ITEMS)
        inter = _make_interaction(user_id=100)
        await _process_trade(cog, inter, buyer, groups[0], "Johnny", 0)
        assert "cannot trade items to yourself" in inter.followup.send.call_args[0][0].lower()
    _run(_test())


def test_trade_process_empty_buyer_char():
    async def _test():
        cog = _make_cog()
        cog.bot.get_cog = MagicMock(return_value=None)
        buyer = _make_buyer()
        groups = _build_groups(SAMPLE_ITEMS)
        inter = _make_interaction()
        await _process_trade(cog, inter, buyer, groups[0], "", 100)
        assert "buyer character name is required" in inter.followup.send.call_args[0][0].lower()
    _run(_test())


def test_trade_process_restricted_item():
    async def _test():
        cog = _make_cog()
        inv_cog = _make_inv_cog()
        cog.bot.cogs["PlayerInventory"] = inv_cog
        cog.bot.get_cog = MagicMock(return_value=None)
        buyer = _make_buyer()
        groups = _build_groups(RESTRICTED_ITEMS)
        inter = _make_interaction(user_id=100)
        with patch("NightCityBot.cogs.player_hub.pi_get_item", new_callable=AsyncMock, return_value=RESTRICTED_ITEMS[0]):
            await _process_trade(cog, inter, buyer, groups[0], "Johnny", 100)
        assert "restricted" in inter.followup.send.call_args[0][0].lower()
    _run(_test())


def test_trade_process_self_trade_blocked_free():
    async def _test():
        cog = _make_cog()
        cog.bot.get_cog = MagicMock(return_value=None)
        buyer = _make_buyer(uid=100, name="TestPlayer")
        groups = _build_groups(SAMPLE_ITEMS)
        inter = _make_interaction(user_id=100)
        await _process_trade(cog, inter, buyer, groups[0], "Jackie", 0)
        assert "cannot trade items to yourself" in inter.followup.send.call_args[0][0].lower()
    _run(_test())


def test_trade_process_item_no_longer_owned():
    async def _test():
        cog = _make_cog()
        inv_cog = _make_inv_cog()
        cog.bot.cogs["PlayerInventory"] = inv_cog
        cog.bot.get_cog = MagicMock(return_value=None)
        buyer = _make_buyer()
        groups = _build_groups(SAMPLE_ITEMS)
        inter = _make_interaction(user_id=100)
        with patch("NightCityBot.cogs.player_hub.pi_get_item", new_callable=AsyncMock, return_value=None):
            await _process_trade(cog, inter, buyer, groups[0], "Johnny", 100)
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
        recipient.roles = []
        select = _find_any_select(view, discord.ui.UserSelect)
        select._values = [recipient]
        with patch("NightCityBot.cogs.player_hub.get_active_characters", new_callable=AsyncMock, return_value=MOCK_ACTIVE_CHARS):
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


def test_give_setup_continue_starts_inline_flow():
    async def _test():
        cog = _make_cog()
        ctx = _make_ctx()
        groups = _build_groups(SAMPLE_ITEMS)
        view = GiveSetupView(cog, ctx, groups)
        view.selected_recipient = _make_buyer()
        view.selected_group_idx = 0
        view.selected_recipient_char_name = "Johnny"
        inter = _make_interaction()
        btn = _find_button(view, "Continue →")
        with patch("NightCityBot.cogs.player_hub._process_give", new_callable=AsyncMock):
            await btn.callback(inter)
        inter.response.defer.assert_called_once()
    _run(_test())


# --- Give Process Function tests ---

def test_give_process_no_guild():
    async def _test():
        cog = _make_cog()
        recipient = _make_buyer()
        groups = _build_groups(SAMPLE_ITEMS)
        inter = _make_interaction()
        inter.guild = None
        await _process_give(cog, inter, recipient, groups[0], "Jackie", "V")
        assert "server" in inter.followup.send.call_args[0][0].lower()
    _run(_test())


def test_give_process_system_disabled():
    async def _test():
        cog = _make_cog()
        control = MagicMock()
        control.is_enabled = MagicMock(return_value=False)
        cog.bot.get_cog = MagicMock(return_value=control)
        recipient = _make_buyer()
        groups = _build_groups(SAMPLE_ITEMS)
        inter = _make_interaction()
        await _process_give(cog, inter, recipient, groups[0], "Jackie", "V")
        assert "offline" in inter.followup.send.call_args[0][0].lower()
    _run(_test())


def test_give_process_no_sender_char():
    async def _test():
        cog = _make_cog()
        cog.bot.get_cog = MagicMock(return_value=None)
        recipient = _make_buyer()
        groups = _build_groups(SAMPLE_ITEMS)
        inter = _make_interaction()
        await _process_give(cog, inter, recipient, groups[0], "Jackie", "")
        assert "character name is required" in inter.followup.send.call_args[0][0].lower()
    _run(_test())


def test_give_process_wrong_character():
    async def _test():
        cog = _make_cog()
        cog.bot.get_cog = MagicMock(return_value=None)
        recipient = _make_buyer()
        groups = _build_groups(SAMPLE_ITEMS)
        inter = _make_interaction(user_id=100)
        await _process_give(cog, inter, recipient, groups[0], "Jackie", "WrongName")
        assert "belongs to character" in inter.followup.send.call_args[0][0].lower()
    _run(_test())


def test_give_process_no_receiver_char():
    async def _test():
        cog = _make_cog()
        cog.bot.get_cog = MagicMock(return_value=None)
        recipient = _make_buyer()
        recipient.roles = []
        groups = _build_groups(SAMPLE_ITEMS)
        inter = _make_interaction(user_id=100)
        await _process_give(cog, inter, recipient, groups[0], "", "V")
        assert "character name is required" in inter.followup.send.call_args[0][0].lower()
    _run(_test())


def test_give_process_success():
    async def _test():
        cog = _make_cog()
        inv_cog = _make_inv_cog()
        cog.bot.cogs["PlayerInventory"] = inv_cog
        cog.bot.get_cog = MagicMock(return_value=None)
        recipient = _make_buyer()
        recipient.roles = []
        groups = _build_groups(SAMPLE_ITEMS)
        inter = _make_interaction(user_id=100)
        with patch("NightCityBot.cogs.player_hub.pi_update_owner", new_callable=AsyncMock, return_value=True):
            with patch("NightCityBot.cogs.player_hub.ih_record_event", new_callable=AsyncMock):
                with patch("NightCityBot.cogs.player_hub._route_log_channel", new_callable=AsyncMock, return_value=None):
                    await _process_give(cog, inter, recipient, groups[0], "Jackie", "V")
        assert "transferred" in inter.followup.send.call_args[0][0].lower() or "✅" in inter.followup.send.call_args[0][0]
    _run(_test())


def test_give_process_transfer_fails():
    async def _test():
        cog = _make_cog()
        cog.bot.get_cog = MagicMock(return_value=None)
        recipient = _make_buyer()
        recipient.roles = []
        groups = _build_groups(SAMPLE_ITEMS)
        inter = _make_interaction(user_id=100)
        with patch("NightCityBot.cogs.player_hub.pi_update_owner", new_callable=AsyncMock, return_value=False):
            await _process_give(cog, inter, recipient, groups[0], "Jackie", "V")
        assert "failed" in inter.followup.send.call_args[0][0].lower()
    _run(_test())


# --- Trade Confirm View ---

def test_trade_confirm_view_accept():
    async def _test():
        view = TradeConfirmView(recipient_id=100, timeout=5)
        inter = _make_interaction()
        btn = _find_button(view, "Accept")
        await btn.callback(inter)
        assert view.accepted is True
    _run(_test())


def test_trade_confirm_view_decline():
    async def _test():
        view = TradeConfirmView(recipient_id=100, timeout=5)
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


def _make_store_owner(uid=200, name="StoreOwner"):
    m = MagicMock(spec=discord.Member)
    m.id = uid
    m.display_name = name
    m.mention = f"<@{uid}>"
    role = MagicMock()
    role.id = 1481022603807166464
    m.roles = [role]
    m.send = AsyncMock()
    return m


def _make_non_owner(uid=201, name="NotOwner"):
    m = MagicMock(spec=discord.Member)
    m.id = uid
    m.display_name = name
    m.mention = f"<@{uid}>"
    m.roles = []
    m.send = AsyncMock()
    return m


class TestSellToStoreButton:
    def test_sell_to_store_button_no_active_chars(self):
        async def _test():
            cog = _make_cog()
            ctx = _make_ctx()
            view = PlayerHubView(cog, ctx)
            inter = _make_interaction()
            with patch("NightCityBot.cogs.player_hub.get_active_characters", new_callable=AsyncMock, return_value=[]):
                btn = _find_button(view, "Sell to Store")
                await btn.callback(inter)
            assert "no active characters" in inter.followup.send.call_args[0][0].lower()
        _run(_test())

    def test_sell_to_store_button_no_items(self):
        async def _test():
            cog = _make_cog()
            ctx = _make_ctx()
            view = PlayerHubView(cog, ctx)
            inter = _make_interaction()
            with patch("NightCityBot.cogs.player_hub.get_active_characters", new_callable=AsyncMock, return_value=MOCK_SELLER_CHARS), \
                 patch("NightCityBot.cogs.player_hub.pi_get_by_owner", new_callable=AsyncMock, return_value=[]):
                btn = _find_button(view, "Sell to Store")
                await btn.callback(inter)
            assert "empty" in inter.followup.send.call_args[0][0].lower()
        _run(_test())

    def test_sell_to_store_button_no_guns(self):
        async def _test():
            cog = _make_cog()
            ctx = _make_ctx()
            view = PlayerHubView(cog, ctx)
            inter = _make_interaction()
            non_gun_items = [{
                "item_id": "uuid-1", "owner_id": "100", "character_name": "V",
                "name": "Medkit", "item_type": "misc", "restriction": "basic",
                "price_paid": 50, "seller_name": "", "acquired_at": "2025-01-01",
                "created_at": "2025-01-01",
            }]
            with patch("NightCityBot.cogs.player_hub.get_active_characters", new_callable=AsyncMock, return_value=MOCK_SELLER_CHARS), \
                 patch("NightCityBot.cogs.player_hub.pi_get_by_owner", new_callable=AsyncMock, return_value=non_gun_items):
                btn = _find_button(view, "Sell to Store")
                await btn.callback(inter)
            assert "no guns" in inter.followup.send.call_args[0][0].lower()
        _run(_test())

    def test_sell_to_store_button_opens_setup(self):
        async def _test():
            cog = _make_cog()
            inv_cog = _make_inv_cog()
            cog.bot.cogs = {"PlayerInventory": inv_cog}
            ctx = _make_ctx()
            view = PlayerHubView(cog, ctx)
            inter = _make_interaction()
            with patch("NightCityBot.cogs.player_hub.get_active_characters", new_callable=AsyncMock, return_value=MOCK_SELLER_CHARS), \
                 patch("NightCityBot.cogs.player_hub.pi_get_by_owner", new_callable=AsyncMock, return_value=SAMPLE_ITEMS):
                btn = _find_button(view, "Sell to Store")
                await btn.callback(inter)
            msg = inter.followup.send.call_args[0][0]
            assert "store owner" in msg.lower()
        _run(_test())

    def test_sell_to_store_system_disabled(self):
        async def _test():
            cog = _make_cog()
            control = MagicMock()
            control.is_enabled = MagicMock(return_value=False)
            cog.bot.get_cog = MagicMock(return_value=control)
            ctx = _make_ctx()
            view = PlayerHubView(cog, ctx)
            inter = _make_interaction()
            btn = _find_button(view, "Sell to Store")
            await btn.callback(inter)
            assert "offline" in inter.followup.send.call_args[0][0].lower()
        _run(_test())


class TestSellToStoreSetupView:
    def test_owner_select_valid_store_owner(self):
        async def _test():
            cog = _make_cog()
            ctx = _make_ctx()
            groups = _build_groups(SAMPLE_ITEMS)
            view = SellToStoreSetupView(cog, ctx, groups)
            inter = _make_interaction()
            owner = _make_store_owner()
            select = _find_any_select(view, discord.ui.UserSelect)
            select._values = [owner]
            await select.callback(inter)
            assert view.selected_store_owner == owner
            assert "✓" in inter.response.send_message.call_args[0][0]
        _run(_test())

    def test_owner_select_not_store_owner(self):
        async def _test():
            cog = _make_cog()
            ctx = _make_ctx()
            groups = _build_groups(SAMPLE_ITEMS)
            view = SellToStoreSetupView(cog, ctx, groups)
            inter = _make_interaction()
            non_owner = _make_non_owner()
            select = _find_any_select(view, discord.ui.UserSelect)
            select._values = [non_owner]
            await select.callback(inter)
            assert view.selected_store_owner is None
            assert "not a gunstore owner" in inter.response.send_message.call_args[0][0]
        _run(_test())

    def test_item_select(self):
        async def _test():
            cog = _make_cog()
            ctx = _make_ctx()
            groups = _build_groups(SAMPLE_ITEMS)
            view = SellToStoreSetupView(cog, ctx, groups)
            inter = _make_interaction()
            inter.data = {"values": ["0"]}
            item_sel = _find_select(view, "gun")
            await item_sel.callback(inter)
            assert view.selected_group_idx == 0
        _run(_test())

    def test_continue_no_owner(self):
        async def _test():
            cog = _make_cog()
            ctx = _make_ctx()
            groups = _build_groups(SAMPLE_ITEMS)
            view = SellToStoreSetupView(cog, ctx, groups)
            inter = _make_interaction()
            btn = _find_button(view, "Continue →")
            await btn.callback(inter)
            assert "store owner" in inter.response.send_message.call_args[0][0].lower()
        _run(_test())

    def test_continue_no_item(self):
        async def _test():
            cog = _make_cog()
            ctx = _make_ctx()
            groups = _build_groups(SAMPLE_ITEMS)
            view = SellToStoreSetupView(cog, ctx, groups)
            view.selected_store_owner = _make_store_owner()
            inter = _make_interaction()
            btn = _find_button(view, "Continue →")
            await btn.callback(inter)
            assert "gun" in inter.response.send_message.call_args[0][0].lower()
        _run(_test())

    def test_continue_self_sell_blocked(self):
        async def _test():
            cog = _make_cog()
            ctx = _make_ctx()
            groups = _build_groups(SAMPLE_ITEMS)
            view = SellToStoreSetupView(cog, ctx, groups)
            view.selected_store_owner = _make_store_owner(uid=100)
            view.selected_group_idx = 0
            inter = _make_interaction(user_id=100)
            btn = _find_button(view, "Continue →")
            await btn.callback(inter)
            assert "yourself" in inter.response.send_message.call_args[0][0].lower()
        _run(_test())

    def test_continue_requires_seller_char(self):
        async def _test():
            cog = _make_cog()
            ctx = _make_ctx()
            groups = _build_groups(SAMPLE_ITEMS)
            view = SellToStoreSetupView(cog, ctx, groups, seller_chars=MOCK_SELLER_CHARS)
            view.selected_store_owner = _make_store_owner()
            view.selected_group_idx = 0
            inter = _make_interaction()
            btn = _find_button(view, "Continue →")
            await btn.callback(inter)
            assert "selling character" in inter.response.send_message.call_args[0][0].lower()
        _run(_test())

    @patch("NightCityBot.cogs.player_hub.collect_text_input", new_callable=AsyncMock, return_value=None)
    def test_continue_starts_inline_flow(self, mock_collect):
        async def _test():
            cog = _make_cog()
            ctx = _make_ctx()
            groups = _build_groups(SAMPLE_ITEMS)
            view = SellToStoreSetupView(cog, ctx, groups)
            view.selected_store_owner = _make_store_owner()
            view.selected_group_idx = 0
            inter = _make_interaction()
            inter.channel_id = 123
            btn = _find_button(view, "Continue →")
            await btn.callback(inter)
            inter.response.defer.assert_called_once()
        _run(_test())

    @patch("NightCityBot.cogs.player_hub.collect_text_input", new_callable=AsyncMock, return_value=None)
    def test_continue_starts_inline_flow_with_seller_char(self, mock_collect):
        async def _test():
            cog = _make_cog()
            ctx = _make_ctx()
            groups = _build_groups(SAMPLE_ITEMS)
            view = SellToStoreSetupView(cog, ctx, groups, seller_chars=MOCK_SELLER_CHARS)
            view.selected_store_owner = _make_store_owner()
            view.selected_group_idx = 0
            view.selected_seller_char_name = "V"
            inter = _make_interaction()
            inter.channel_id = 123
            btn = _find_button(view, "Continue →")
            await btn.callback(inter)
            inter.response.defer.assert_called_once()
        _run(_test())

    def test_interaction_check(self):
        async def _test():
            cog = _make_cog()
            ctx = _make_ctx(author_id=100)
            groups = _build_groups(SAMPLE_ITEMS)
            view = SellToStoreSetupView(cog, ctx, groups)
            inter_ok = _make_interaction(user_id=100)
            inter_bad = _make_interaction(user_id=999)
            assert await view.interaction_check(inter_ok) is True
            assert await view.interaction_check(inter_bad) is False
        _run(_test())


class TestSellToStoreProcessFunction:
    def _make_setup(self, store_owner=None, group=None):
        cog = _make_cog()
        inv_cog = _make_inv_cog()
        inv_cog.unbelievaboat = MagicMock()
        cog.bot.cogs = {"PlayerInventory": inv_cog, "GunsShopCog": MagicMock()}
        if store_owner is None:
            store_owner = _make_store_owner()
        if group is None:
            group = _build_groups(SAMPLE_ITEMS)[0]
        return cog, store_owner, group

    def test_zero_price_allowed(self):
        async def _test():
            cog, owner, group = self._make_setup()
            guns_cog = MagicMock()
            guns_cog._store_id = MagicMock(return_value="999:200")
            cog.bot.cogs = {"PlayerInventory": _make_inv_cog(), "GunsShopCog": guns_cog}
            inter = _make_interaction()
            with patch("NightCityBot.cogs.player_hub.pi_get_item", new_callable=AsyncMock, return_value={"owner_id": "100"}), \
                 patch("NightCityBot.cogs.player_hub.StoreBuyConfirmView") as MockConfirm:
                confirm_inst = MagicMock()
                confirm_inst.accepted = False
                confirm_inst.wait = AsyncMock()
                MockConfirm.return_value = confirm_inst
                await _process_sell_to_store(cog, inter, owner, group, None, 0)
            assert inter.followup.send.called
        _run(_test())

    def test_self_sell_blocked(self):
        async def _test():
            owner = _make_store_owner(uid=100)
            cog, _, group = self._make_setup(store_owner=owner)
            inter = _make_interaction(user_id=100)
            await _process_sell_to_store(cog, inter, owner, group, None, 1000)
            assert "yourself" in inter.followup.send.call_args[0][0].lower()
        _run(_test())

    def test_item_no_longer_owned(self):
        async def _test():
            cog, owner, group = self._make_setup()
            inter = _make_interaction()
            with patch("NightCityBot.cogs.player_hub.pi_get_item", new_callable=AsyncMock, return_value=None):
                await _process_sell_to_store(cog, inter, owner, group, None, 1000)
            assert "no longer" in inter.followup.send.call_args[0][0].lower()
        _run(_test())

    def test_guns_cog_unavailable(self):
        async def _test():
            cog, owner, group = self._make_setup()
            cog.bot.cogs = {"PlayerInventory": _make_inv_cog()}
            inter = _make_interaction()
            with patch("NightCityBot.cogs.player_hub.pi_get_item", new_callable=AsyncMock, return_value={"owner_id": "100"}):
                await _process_sell_to_store(cog, inter, owner, group, None, 1000)
            assert "unavailable" in inter.followup.send.call_args[0][0].lower()
        _run(_test())

    def test_store_owner_dm_blocked(self):
        async def _test():
            owner = _make_store_owner()
            owner.send = AsyncMock(side_effect=discord.Forbidden(MagicMock(), ""))
            cog, _, group = self._make_setup(store_owner=owner)
            guns_cog = MagicMock()
            guns_cog._store_id = MagicMock(return_value="999:200")
            cog.bot.cogs = {"PlayerInventory": _make_inv_cog(), "GunsShopCog": guns_cog}
            inter = _make_interaction()
            with patch("NightCityBot.cogs.player_hub.pi_get_item", new_callable=AsyncMock, return_value={"owner_id": "100"}):
                await _process_sell_to_store(cog, inter, owner, group, None, 1000)
            assert "dm" in inter.followup.send.call_args[0][0].lower()
        _run(_test())

    def test_store_owner_declines(self):
        async def _test():
            owner = _make_store_owner()
            owner.send = AsyncMock()
            cog, _, group = self._make_setup(store_owner=owner)
            guns_cog = MagicMock()
            guns_cog._store_id = MagicMock(return_value="999:200")
            cog.bot.cogs = {"PlayerInventory": _make_inv_cog(), "GunsShopCog": guns_cog}
            inter = _make_interaction()
            with patch("NightCityBot.cogs.player_hub.pi_get_item", new_callable=AsyncMock, return_value={"owner_id": "100"}), \
                 patch("NightCityBot.cogs.player_hub.StoreBuyConfirmView") as MockConfirm:
                confirm_inst = MagicMock()
                confirm_inst.accepted = False
                confirm_inst.wait = AsyncMock()
                MockConfirm.return_value = confirm_inst
                await _process_sell_to_store(cog, inter, owner, group, None, 1000)
            assert "declined" in inter.followup.send.call_args[0][0].lower()
        _run(_test())

    def test_successful_sale(self):
        async def _test():
            owner = _make_store_owner()
            dm_msg = MagicMock()
            dm_msg.edit = AsyncMock()
            owner.send = AsyncMock(return_value=dm_msg)
            cog, _, group = self._make_setup(store_owner=owner)

            inv_cog = _make_inv_cog()
            inv_cog.unbelievaboat.get_balance = AsyncMock(return_value={"cash": 5000, "bank": 0})
            inv_cog.unbelievaboat.update_balance = AsyncMock(return_value=True)

            guns_cog = MagicMock()
            guns_cog._store_id = MagicMock(return_value="999:200")
            guns_cog.lock = asyncio.Lock()
            guns_cog._load_state = AsyncMock(return_value={"stores": {}})
            guns_cog._save_state = AsyncMock()
            cog.bot.cogs = {"PlayerInventory": inv_cog, "GunsShopCog": guns_cog}

            inter = _make_interaction()
            with patch("NightCityBot.cogs.player_hub.pi_get_item", new_callable=AsyncMock, return_value={"owner_id": "100"}), \
                 patch("NightCityBot.cogs.player_hub.pi_delete_item", new_callable=AsyncMock, return_value=True), \
                 patch("NightCityBot.cogs.player_hub.StoreBuyConfirmView") as MockConfirm, \
                 patch("NightCityBot.cogs.player_hub._route_log_channel", new_callable=AsyncMock, return_value=None), \
                 patch("NightCityBot.cogs.player_hub.ih_record_event", new_callable=AsyncMock):
                confirm_inst = MagicMock()
                confirm_inst.accepted = True
                confirm_inst.wait = AsyncMock()
                MockConfirm.return_value = confirm_inst
                await _process_sell_to_store(cog, inter, owner, group, None, 1000)

            last_msg = inter.followup.send.call_args[0][0]
            assert "✅" in last_msg
            assert "Sold" in last_msg
            guns_cog._save_state.assert_called_once()
        _run(_test())

    def test_successful_sale_free(self):
        async def _test():
            owner = _make_store_owner()
            dm_msg = MagicMock()
            dm_msg.edit = AsyncMock()
            owner.send = AsyncMock(return_value=dm_msg)
            cog, _, group = self._make_setup(store_owner=owner)

            guns_cog = MagicMock()
            guns_cog._store_id = MagicMock(return_value="999:200")
            guns_cog.lock = asyncio.Lock()
            guns_cog._load_state = AsyncMock(return_value={"stores": {}})
            guns_cog._save_state = AsyncMock()
            cog.bot.cogs = {"PlayerInventory": _make_inv_cog(), "GunsShopCog": guns_cog}

            inter = _make_interaction()
            with patch("NightCityBot.cogs.player_hub.pi_get_item", new_callable=AsyncMock, return_value={"owner_id": "100"}), \
                 patch("NightCityBot.cogs.player_hub.pi_delete_item", new_callable=AsyncMock, return_value=True), \
                 patch("NightCityBot.cogs.player_hub.StoreBuyConfirmView") as MockConfirm, \
                 patch("NightCityBot.cogs.player_hub._route_log_channel", new_callable=AsyncMock, return_value=None), \
                 patch("NightCityBot.cogs.player_hub.ih_record_event", new_callable=AsyncMock):
                confirm_inst = MagicMock()
                confirm_inst.accepted = True
                confirm_inst.wait = AsyncMock()
                MockConfirm.return_value = confirm_inst
                await _process_sell_to_store(cog, inter, owner, group, None, 0)

            last_msg = inter.followup.send.call_args[0][0]
            assert "✅" in last_msg
            assert "free" in last_msg
        _run(_test())

    def test_delete_item_fails_refund(self):
        async def _test():
            owner = _make_store_owner()
            dm_msg = MagicMock()
            dm_msg.edit = AsyncMock()
            owner.send = AsyncMock(return_value=dm_msg)
            cog, _, group = self._make_setup(store_owner=owner)

            inv_cog = _make_inv_cog()
            inv_cog.unbelievaboat.get_balance = AsyncMock(return_value={"cash": 5000, "bank": 0})
            inv_cog.unbelievaboat.update_balance = AsyncMock(return_value=True)

            guns_cog = MagicMock()
            guns_cog._store_id = MagicMock(return_value="999:200")
            cog.bot.cogs = {"PlayerInventory": inv_cog, "GunsShopCog": guns_cog}

            inter = _make_interaction()
            with patch("NightCityBot.cogs.player_hub.pi_get_item", new_callable=AsyncMock, return_value={"owner_id": "100"}), \
                 patch("NightCityBot.cogs.player_hub.pi_delete_item", new_callable=AsyncMock, return_value=False), \
                 patch("NightCityBot.cogs.player_hub.StoreBuyConfirmView") as MockConfirm:
                confirm_inst = MagicMock()
                confirm_inst.accepted = True
                confirm_inst.wait = AsyncMock()
                MockConfirm.return_value = confirm_inst
                await _process_sell_to_store(cog, inter, owner, group, None, 1000)

            last_msg = inter.followup.send.call_args[0][0]
            assert "failed" in last_msg.lower()
            assert inv_cog.unbelievaboat.update_balance.call_count >= 3
        _run(_test())

    def test_store_owner_cant_afford(self):
        async def _test():
            owner = _make_store_owner()
            dm_msg = MagicMock()
            dm_msg.edit = AsyncMock()
            owner.send = AsyncMock(return_value=dm_msg)
            cog, _, group = self._make_setup(store_owner=owner)

            inv_cog = _make_inv_cog()
            inv_cog.unbelievaboat.get_balance = AsyncMock(return_value={"cash": 100, "bank": 50})
            inv_cog.unbelievaboat.update_balance = AsyncMock(return_value=True)

            guns_cog = MagicMock()
            guns_cog._store_id = MagicMock(return_value="999:200")
            cog.bot.cogs = {"PlayerInventory": inv_cog, "GunsShopCog": guns_cog}

            inter = _make_interaction()
            with patch("NightCityBot.cogs.player_hub.pi_get_item", new_callable=AsyncMock, return_value={"owner_id": "100"}), \
                 patch("NightCityBot.cogs.player_hub.StoreBuyConfirmView") as MockConfirm:
                confirm_inst = MagicMock()
                confirm_inst.accepted = True
                confirm_inst.wait = AsyncMock()
                MockConfirm.return_value = confirm_inst
                await _process_sell_to_store(cog, inter, owner, group, None, 10000)

            assert "cannot afford" in inter.followup.send.call_args[0][0].lower()
        _run(_test())


class TestStoreBuyConfirmView:
    def test_accept(self):
        async def _test():
            view = StoreBuyConfirmView(recipient_id=100)
            inter = _make_interaction()
            btn = _find_button(view, "Buy")
            await btn.callback(inter)
            assert view.accepted is True
        _run(_test())

    def test_decline(self):
        async def _test():
            view = StoreBuyConfirmView(recipient_id=100)
            inter = _make_interaction()
            btn = _find_button(view, "Decline")
            await btn.callback(inter)
            assert view.accepted is False
        _run(_test())


class TestSellToStoreEdgeCases:
    def test_save_state_failure_creates_pending_transfer(self):
        async def _test():
            owner = _make_store_owner()
            dm_msg = MagicMock()
            dm_msg.edit = AsyncMock()
            owner.send = AsyncMock(return_value=dm_msg)
            cog = _make_cog()
            inv_cog = _make_inv_cog()
            inv_cog.unbelievaboat.get_balance = AsyncMock(return_value={"cash": 5000, "bank": 0})
            inv_cog.unbelievaboat.update_balance = AsyncMock(return_value=True)
            guns_cog = MagicMock()
            guns_cog._store_id = MagicMock(return_value="999:200")
            guns_cog.lock = asyncio.Lock()
            guns_cog._load_state = AsyncMock(return_value={"stores": {}})
            guns_cog._save_state = AsyncMock(side_effect=RuntimeError("disk full"))
            cog.bot.cogs = {"PlayerInventory": inv_cog, "GunsShopCog": guns_cog}
            group = _build_groups(SAMPLE_ITEMS)[0]
            inter = _make_interaction()
            with patch("NightCityBot.cogs.player_hub.pi_get_item", new_callable=AsyncMock, return_value={"owner_id": "100"}), \
                 patch("NightCityBot.cogs.player_hub.pi_delete_item", new_callable=AsyncMock, return_value=True), \
                 patch("NightCityBot.cogs.player_hub.StoreBuyConfirmView") as MockConfirm, \
                 patch("NightCityBot.cogs.player_hub.pt_create", new_callable=AsyncMock) as mock_pt, \
                 patch("NightCityBot.cogs.player_hub._log_channel", new_callable=AsyncMock, return_value=None):
                confirm_inst = MagicMock()
                confirm_inst.accepted = True
                confirm_inst.wait = AsyncMock()
                MockConfirm.return_value = confirm_inst
                await _process_sell_to_store(cog, inter, owner, group, None, 1000)
            mock_pt.assert_called_once()
            last_msg = inter.followup.send.call_args[0][0]
            assert "failed" in last_msg.lower()
        _run(_test())

    def test_owner_select_role_not_configured(self):
        async def _test():
            cog = _make_cog()
            ctx = _make_ctx()
            groups = _build_groups(SAMPLE_ITEMS)
            view = SellToStoreSetupView(cog, ctx, groups)
            inter = _make_interaction()
            owner = _make_store_owner()
            select = _find_any_select(view, discord.ui.UserSelect)
            select._values = [owner]
            with patch.object(type(cog), '__module__', 'NightCityBot.cogs.player_hub'):
                import config as cfg
                original = getattr(cfg, "WHOLESALER_STORE_ROLE_IDS", None)
                try:
                    cfg.WHOLESALER_STORE_ROLE_IDS = None
                    await select.callback(inter)
                finally:
                    if original is not None:
                        cfg.WHOLESALER_STORE_ROLE_IDS = original
            assert "not configured" in inter.response.send_message.call_args[0][0].lower()
        _run(_test())


# --- Character Lifecycle Tests ---


class TestCreateCharacterButton:
    def test_create_char_success(self):
        async def _test():
            cog = _make_cog()
            cog.bot.wait_for = AsyncMock()
            msg_mock = MagicMock()
            msg_mock.content = "V"
            msg_mock.delete = AsyncMock()
            cog.bot.wait_for.return_value = msg_mock
            ctx = _make_ctx()
            view = PlayerHubView(cog, ctx)
            inter = _make_interaction()
            inter.channel = MagicMock()
            inter.channel.id = 123
            btn = _find_button(view, "Create Character")
            with patch("NightCityBot.cogs.player_hub.character_name_exists", new_callable=AsyncMock, return_value=False), \
                 patch("NightCityBot.cogs.player_hub.create_character", new_callable=AsyncMock, return_value={"character_id": "uuid-new", "name": "V"}), \
                 patch("NightCityBot.cogs.player_hub._log_channel", new_callable=AsyncMock, return_value=None):
                await btn.callback(inter)
            last_msg = inter.followup.send.call_args[0][0]
            assert "✅" in last_msg
            assert "V" in last_msg
        _run(_test())

    def test_create_char_timeout(self):
        async def _test():
            cog = _make_cog()
            cog.bot.wait_for = AsyncMock(side_effect=asyncio.TimeoutError)
            ctx = _make_ctx()
            view = PlayerHubView(cog, ctx)
            inter = _make_interaction()
            inter.channel = MagicMock()
            inter.channel.id = 123
            btn = _find_button(view, "Create Character")
            await btn.callback(inter)
            last_msg = inter.followup.send.call_args[0][0]
            assert "timed out" in last_msg.lower()
        _run(_test())

    def test_create_char_empty_name(self):
        async def _test():
            cog = _make_cog()
            msg_mock = MagicMock()
            msg_mock.content = ""
            msg_mock.delete = AsyncMock()
            cog.bot.wait_for = AsyncMock(return_value=msg_mock)
            ctx = _make_ctx()
            view = PlayerHubView(cog, ctx)
            inter = _make_interaction()
            inter.channel = MagicMock()
            inter.channel.id = 123
            btn = _find_button(view, "Create Character")
            await btn.callback(inter)
            last_msg = inter.followup.send.call_args[0][0]
            assert "cannot be empty" in last_msg.lower()
        _run(_test())

    def test_create_char_too_long(self):
        async def _test():
            cog = _make_cog()
            msg_mock = MagicMock()
            msg_mock.content = "A" * 65
            msg_mock.delete = AsyncMock()
            cog.bot.wait_for = AsyncMock(return_value=msg_mock)
            ctx = _make_ctx()
            view = PlayerHubView(cog, ctx)
            inter = _make_interaction()
            inter.channel = MagicMock()
            inter.channel.id = 123
            btn = _find_button(view, "Create Character")
            await btn.callback(inter)
            last_msg = inter.followup.send.call_args[0][0]
            assert "64 characters" in last_msg.lower()
        _run(_test())

    def test_create_char_duplicate(self):
        async def _test():
            cog = _make_cog()
            msg_mock = MagicMock()
            msg_mock.content = "V"
            msg_mock.delete = AsyncMock()
            cog.bot.wait_for = AsyncMock(return_value=msg_mock)
            ctx = _make_ctx()
            view = PlayerHubView(cog, ctx)
            inter = _make_interaction()
            inter.channel = MagicMock()
            inter.channel.id = 123
            btn = _find_button(view, "Create Character")
            with patch("NightCityBot.cogs.player_hub.character_name_exists", new_callable=AsyncMock, return_value=True):
                await btn.callback(inter)
            last_msg = inter.followup.send.call_args[0][0]
            assert "already have" in last_msg.lower()
        _run(_test())


class TestManageCharactersView:
    def test_manage_chars_button_opens_view(self):
        async def _test():
            cog = _make_cog()
            ctx = _make_ctx()
            view = PlayerHubView(cog, ctx)
            inter = _make_interaction()
            btn = _find_button(view, "Manage Characters")
            await btn.callback(inter)
            call_kwargs = inter.followup.send.call_args.kwargs
            assert isinstance(call_kwargs["view"], ManageCharactersView)
        _run(_test())

    def test_deactivate_no_active_chars(self):
        async def _test():
            cog = _make_cog()
            ctx = _make_ctx()
            view = ManageCharactersView(cog, ctx)
            inter = _make_interaction()
            btn = _find_button(view, "Deactivate")
            with patch("NightCityBot.cogs.player_hub.get_active_characters", new_callable=AsyncMock, return_value=[]):
                await btn.callback(inter)
            assert "no active characters" in inter.followup.send.call_args[0][0].lower()
        _run(_test())

    def test_deactivate_shows_view(self):
        async def _test():
            cog = _make_cog()
            ctx = _make_ctx()
            view = ManageCharactersView(cog, ctx)
            inter = _make_interaction()
            btn = _find_button(view, "Deactivate")
            with patch("NightCityBot.cogs.player_hub.get_active_characters", new_callable=AsyncMock, return_value=MOCK_ACTIVE_CHARS):
                await btn.callback(inter)
            call_kwargs = inter.followup.send.call_args.kwargs
            assert isinstance(call_kwargs["view"], DeactivateCharacterView)
        _run(_test())

    def test_reactivate_no_inactive_chars(self):
        async def _test():
            cog = _make_cog()
            ctx = _make_ctx()
            view = ManageCharactersView(cog, ctx)
            inter = _make_interaction()
            btn = _find_button(view, "Reactivate")
            with patch("NightCityBot.cogs.player_hub.get_inactive_characters", new_callable=AsyncMock, return_value=[]):
                await btn.callback(inter)
            assert "no inactive characters" in inter.followup.send.call_args[0][0].lower()
        _run(_test())

    def test_reactivate_shows_view(self):
        async def _test():
            cog = _make_cog()
            ctx = _make_ctx()
            view = ManageCharactersView(cog, ctx)
            inter = _make_interaction()
            btn = _find_button(view, "Reactivate")
            with patch("NightCityBot.cogs.player_hub.get_inactive_characters", new_callable=AsyncMock, return_value=MOCK_INACTIVE_CHARS):
                await btn.callback(inter)
            call_kwargs = inter.followup.send.call_args.kwargs
            assert isinstance(call_kwargs["view"], ReactivateCharacterView)
        _run(_test())


class TestDeactivateCharacterView:
    def test_select_and_confirm(self):
        async def _test():
            cog = _make_cog()
            ctx = _make_ctx()
            view = DeactivateCharacterView(cog, ctx, MOCK_ACTIVE_CHARS)
            inter = _make_interaction()
            inter.data = {"values": ["uuid-char-1"]}
            char_sel = _find_select(view, "deactivate")
            await char_sel.callback(inter)
            assert view.selected_char_id == "uuid-char-1"
            assert view.selected_char_name == "Johnny"
            inter2 = _make_interaction()
            btn = _find_button(view, "Confirm Deactivate")
            with patch("NightCityBot.cogs.player_hub.deactivate_character", new_callable=AsyncMock, return_value=True), \
                 patch("NightCityBot.cogs.player_hub._log_channel", new_callable=AsyncMock, return_value=None):
                await btn.callback(inter2)
            assert "deactivated" in inter2.followup.send.call_args[0][0].lower()
        _run(_test())

    def test_confirm_without_selection(self):
        async def _test():
            cog = _make_cog()
            ctx = _make_ctx()
            view = DeactivateCharacterView(cog, ctx, MOCK_ACTIVE_CHARS)
            inter = _make_interaction()
            btn = _find_button(view, "Confirm Deactivate")
            await btn.callback(inter)
            assert "select a character" in inter.response.send_message.call_args[0][0].lower()
        _run(_test())

    def test_deactivate_fails(self):
        async def _test():
            cog = _make_cog()
            ctx = _make_ctx()
            view = DeactivateCharacterView(cog, ctx, MOCK_ACTIVE_CHARS)
            view.selected_char_id = "uuid-char-1"
            view.selected_char_name = "Johnny"
            inter = _make_interaction()
            btn = _find_button(view, "Confirm Deactivate")
            with patch("NightCityBot.cogs.player_hub.deactivate_character", new_callable=AsyncMock, return_value=False):
                await btn.callback(inter)
            assert "failed" in inter.followup.send.call_args[0][0].lower()
        _run(_test())


class TestReactivateCharacterView:
    def test_select_and_confirm(self):
        async def _test():
            cog = _make_cog()
            ctx = _make_ctx()
            view = ReactivateCharacterView(cog, ctx, MOCK_INACTIVE_CHARS)
            inter = _make_interaction()
            inter.data = {"values": ["uuid-char-3"]}
            char_sel = _find_select(view, "reactivate")
            await char_sel.callback(inter)
            assert view.selected_char_id == "uuid-char-3"
            assert view.selected_char_name == "Jackie"
            inter2 = _make_interaction()
            btn = _find_button(view, "Confirm Reactivate")
            with patch("NightCityBot.cogs.player_hub.reactivate_character", new_callable=AsyncMock, return_value=True), \
                 patch("NightCityBot.cogs.player_hub._log_channel", new_callable=AsyncMock, return_value=None):
                await btn.callback(inter2)
            assert "reactivated" in inter2.followup.send.call_args[0][0].lower()
        _run(_test())

    def test_confirm_without_selection(self):
        async def _test():
            cog = _make_cog()
            ctx = _make_ctx()
            view = ReactivateCharacterView(cog, ctx, MOCK_INACTIVE_CHARS)
            inter = _make_interaction()
            btn = _find_button(view, "Confirm Reactivate")
            await btn.callback(inter)
            assert "select a character" in inter.response.send_message.call_args[0][0].lower()
        _run(_test())

    def test_reactivate_fails(self):
        async def _test():
            cog = _make_cog()
            ctx = _make_ctx()
            view = ReactivateCharacterView(cog, ctx, MOCK_INACTIVE_CHARS)
            view.selected_char_id = "uuid-char-3"
            view.selected_char_name = "Jackie"
            inter = _make_interaction()
            btn = _find_button(view, "Confirm Reactivate")
            with patch("NightCityBot.cogs.player_hub.reactivate_character", new_callable=AsyncMock, return_value=False):
                await btn.callback(inter)
            assert "failed" in inter.followup.send.call_args[0][0].lower()
        _run(_test())


class TestTradeCharacterSelection:
    def test_buyer_no_active_chars_blocks(self):
        async def _test():
            cog = _make_cog()
            ctx = _make_ctx()
            groups = _build_groups(SAMPLE_ITEMS)
            view = TradeSetupView(cog, ctx, groups)
            inter = _make_interaction()
            buyer = _make_buyer()
            select = _find_any_select(view, discord.ui.UserSelect)
            select._values = [buyer]
            with patch("NightCityBot.cogs.player_hub.get_active_characters", new_callable=AsyncMock, return_value=[]):
                await select.callback(inter)
            assert view.selected_buyer is None
            assert "no active characters" in inter.response.send_message.call_args[0][0].lower()
        _run(_test())


class TestGiveCharacterSelection:
    def test_recipient_no_active_chars_blocks(self):
        async def _test():
            cog = _make_cog()
            ctx = _make_ctx()
            groups = _build_groups(SAMPLE_ITEMS)
            view = GiveSetupView(cog, ctx, groups)
            inter = _make_interaction()
            recipient = _make_buyer(uid=222, name="Recipient")
            recipient.roles = []
            select = _find_any_select(view, discord.ui.UserSelect)
            select._values = [recipient]
            with patch("NightCityBot.cogs.player_hub.get_active_characters", new_callable=AsyncMock, return_value=[]):
                await select.callback(inter)
            assert view.selected_recipient is None
            assert "no active characters" in inter.response.send_message.call_args[0][0].lower()
        _run(_test())

    def test_recipient_ripperdoc_skips_char_select(self):
        async def _test():
            cog = _make_cog()
            ctx = _make_ctx()
            groups = _build_groups(SAMPLE_ITEMS)
            view = GiveSetupView(cog, ctx, groups)
            inter = _make_interaction()
            recipient = _make_buyer(uid=222, name="DocRipper")
            rd_role = MagicMock()
            rd_role.id = 1356028868103897156
            recipient.roles = [rd_role]
            select = _find_any_select(view, discord.ui.UserSelect)
            select._values = [recipient]
            await select.callback(inter)
            assert view.selected_recipient == recipient
            assert view._is_ripperdoc_recipient is True
            assert "ripperdoc" in inter.response.send_message.call_args[0][0].lower()
        _run(_test())


# --- Inventory Character Filter Tests ---

MULTI_CHAR_ITEMS = [
    {
        "item_id": "uuid-1", "owner_id": "100", "character_name": "V",
        "name": "Pistol", "item_type": "gun", "restriction": "basic",
        "price_paid": 100, "seller_name": "", "acquired_at": "2025-01-01",
        "created_at": "2025-01-01",
    },
    {
        "item_id": "uuid-2", "owner_id": "100", "character_name": "Johnny",
        "name": "Katana", "item_type": "gun", "restriction": "basic",
        "price_paid": 200, "seller_name": "", "acquired_at": "2025-01-01",
        "created_at": "2025-01-01",
    },
]


class TestInventoryCharFilter:
    def test_view_inv_multi_chars_shows_filter(self):
        async def _test():
            cog = _make_cog()
            inv_cog = _make_inv_cog()
            cog.bot.cogs["PlayerInventory"] = inv_cog
            ctx = _make_ctx()
            view = PlayerHubView(cog, ctx)
            inter = _make_interaction()
            with patch("NightCityBot.cogs.player_hub.pi_get_by_owner", new_callable=AsyncMock, return_value=MULTI_CHAR_ITEMS):
                btn = _find_button(view, "View Inventory")
                await btn.callback(inter)
            call_kwargs = inter.followup.send.call_args.kwargs
            assert isinstance(call_kwargs["view"], InventoryCharFilterView)
        _run(_test())

    def test_view_inv_single_char_shows_embed(self):
        async def _test():
            cog = _make_cog()
            inv_cog = _make_inv_cog()
            cog.bot.cogs["PlayerInventory"] = inv_cog
            ctx = _make_ctx()
            view = PlayerHubView(cog, ctx)
            inter = _make_interaction()
            with patch("NightCityBot.cogs.player_hub.pi_get_by_owner", new_callable=AsyncMock, return_value=SAMPLE_ITEMS):
                btn = _find_button(view, "View Inventory")
                await btn.callback(inter)
            call_kwargs = inter.followup.send.call_args.kwargs
            assert "embed" in call_kwargs
        _run(_test())

    def test_filter_view_select_character(self):
        async def _test():
            cog = _make_cog()
            inv_cog = _make_inv_cog()
            ctx = _make_ctx()
            view = InventoryCharFilterView(cog, ctx, MULTI_CHAR_ITEMS, inv_cog, ["Johnny", "V"])
            inter = _make_interaction()
            with patch.object(type(view.char_select), "values", new_callable=lambda: property(lambda s: ["V"])):
                await view._on_char_select(inter)
            call_kwargs = inter.followup.send.call_args.kwargs
            assert "embed" in call_kwargs
            assert "V" in call_kwargs["embed"].title
        _run(_test())

    def test_filter_view_select_all(self):
        async def _test():
            cog = _make_cog()
            inv_cog = _make_inv_cog()
            ctx = _make_ctx()
            view = InventoryCharFilterView(cog, ctx, MULTI_CHAR_ITEMS, inv_cog, ["Johnny", "V"])
            inter = _make_interaction()
            with patch.object(type(view.char_select), "values", new_callable=lambda: property(lambda s: ["__all__"])):
                await view._on_char_select(inter)
            call_kwargs = inter.followup.send.call_args.kwargs
            assert "embed" in call_kwargs

    def test_build_inventory_embed_with_filter(self):
        inv_cog = _make_inv_cog()
        embed = _build_inventory_embed("TestUser", MULTI_CHAR_ITEMS, inv_cog, "V")
        assert "V" in embed.title
        assert "1 total item" in embed.footer.text

    def test_build_inventory_embed_no_filter(self):
        inv_cog = _make_inv_cog()
        embed = _build_inventory_embed("TestUser", MULTI_CHAR_ITEMS, inv_cog)
        assert "TestUser" in embed.title
        assert "2 total item" in embed.footer.text
