"""End-to-end flow tests that chain multiple interaction steps together.

Each test simulates a complete user journey: hub command -> button click ->
dropdown selection -> text input -> final confirmation, asserting correctness
at every intermediate step.
"""

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from NightCityBot.cogs.fixer_hub import (
    FixerHubCog,
    FixerTopView,
    PlayerSubView,
    PlayerAddItemPickerView,
)
from NightCityBot.cogs.player_hub import (
    PlayerHubCog,
    PlayerHubView,
    TradeSetupView,
    TradeConfirmView,
    _process_trade,
)
from NightCityBot.cogs.ripperdoc_hub import (
    RipperdocHub,
    RipperdocMenuView,
    SellSetupView,
    DMConfirmView,
)
from NightCityBot.cogs.gunstore_hub import (
    GunstoreHub,
    GunstoreMenuView,
    GunSellSetupView,
    GunDMConfirmView,
    InlineApproveView,
)
from NightCityBot.cogs.admin_shop import (
    AdminShopCog,
    AdminShopMenuView,
    WholesaleClearConfirmView,
)

MOCK_CHARS_BUYER = [
    {"character_id": "char-b1", "user_id": "200", "name": "Johnny", "active": True},
]
MOCK_CHARS_PLAYER = [
    {"character_id": "char-p1", "user_id": "300", "name": "V", "active": True},
]
MOCK_CHARS_PATIENT = [
    {"character_id": "char-pat1", "user_id": "400", "name": "Judy", "active": True},
]
MOCK_CHARS_CUSTOMER = [
    {"character_id": "char-cust1", "user_id": "500", "name": "Panam", "active": True},
]

SAMPLE_ITEMS = [
    {
        "item_id": "uuid-item-1",
        "owner_id": "100",
        "character_name": "V",
        "name": "Katana",
        "item_type": "melee",
        "restriction": "basic",
        "description": "",
        "price_paid": 500,
    }
]


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_bot():
    bot = MagicMock()
    bot.get_channel = MagicMock(return_value=None)
    bot.fetch_channel = AsyncMock(side_effect=Exception("no channel"))
    bot.cogs = {}
    return bot


def _make_ctx(author_id=100, guild_id=999, admin=False):
    ctx = MagicMock()
    ctx.send = AsyncMock()
    ctx.author = MagicMock(spec=discord.Member)
    ctx.author.id = author_id
    ctx.author.display_name = f"User{author_id}"
    ctx.author.mention = f"<@{author_id}>"
    ctx.author.roles = []
    ctx.author.guild_permissions = MagicMock()
    ctx.author.guild_permissions.administrator = admin
    ctx.guild = MagicMock()
    ctx.guild.id = guild_id
    ctx.guild.get_member = MagicMock(return_value=None)
    ctx.guild.fetch_member = AsyncMock(return_value=None)
    ctx.message = MagicMock()
    ctx.message.delete = AsyncMock()
    ctx.interaction = None
    ctx.channel = MagicMock()
    ctx.channel.id = 12345
    return ctx


def _make_interaction(user_id=100, guild_id=999):
    inter = MagicMock(spec=discord.Interaction)
    inter.user = MagicMock(spec=discord.Member)
    inter.user.id = user_id
    inter.user.display_name = f"User{user_id}"
    inter.user.mention = f"<@{user_id}>"
    inter.response = MagicMock()
    inter.response.defer = AsyncMock()
    inter.response.send_message = AsyncMock()
    inter.response.edit_message = AsyncMock()
    inter.followup = MagicMock()
    inter.followup.send = AsyncMock()
    inter.message = MagicMock()
    inter.message.delete = AsyncMock()
    inter.guild = MagicMock()
    inter.guild.id = guild_id
    inter.guild.get_member = MagicMock(return_value=None)
    inter.guild.fetch_member = AsyncMock(return_value=None)
    inter.channel_id = 12345
    inter.data = {}
    inter.edit_original_response = AsyncMock()
    inter.client = MagicMock()
    inter.client.get_cog = MagicMock(return_value=None)
    inter.client.get_guild = MagicMock(return_value=inter.guild)
    return inter


def _make_member(member_id, name="Member"):
    m = MagicMock(spec=discord.Member)
    m.id = member_id
    m.display_name = name
    m.mention = f"<@{member_id}>"
    m.roles = []
    m.add_roles = AsyncMock()
    m.remove_roles = AsyncMock()
    m.guild_permissions = MagicMock()
    m.guild_permissions.administrator = False
    m.send = AsyncMock(return_value=MagicMock(edit=AsyncMock()))
    return m


def _find_button(view, label):
    for child in view.children:
        if isinstance(child, discord.ui.Button) and child.label == label:
            return child
    raise ValueError(f"No button with label {label!r} in {type(view).__name__}")


def _find_user_select(view):
    for child in view.children:
        if isinstance(child, discord.ui.UserSelect):
            return child
    raise ValueError(f"No UserSelect in {type(view).__name__}")


def _find_select(view, placeholder=None):
    for child in view.children:
        if isinstance(child, discord.ui.Select):
            if placeholder is None or (child.placeholder and placeholder.lower() in child.placeholder.lower()):
                return child
    raise ValueError(f"No Select with placeholder containing {placeholder!r}")


class TestFlowA_FixerAddItem:
    """Flow A: Fixer Top -> Player -> Add Item -> select user -> select char -> text -> item created."""

    @patch("NightCityBot.cogs.fixer_hub.ih_record_event", new_callable=AsyncMock)
    @patch("NightCityBot.cogs.fixer_hub.pi_add_item", new_callable=AsyncMock, return_value=True)
    @patch("NightCityBot.cogs.fixer_hub.ensure_character_active", new_callable=AsyncMock, return_value=True)
    @patch("NightCityBot.cogs.fixer_hub.collect_text_input", new_callable=AsyncMock)
    @patch("NightCityBot.cogs.fixer_hub.get_active_characters", new_callable=AsyncMock)
    def test_full_add_item_flow(self, mock_chars, mock_text, mock_ensure, mock_add, mock_history):
        mock_chars.return_value = MOCK_CHARS_PLAYER
        mock_text.return_value = "Militech Pistol, gun, 1, 0, basic, high, power"

        async def _test():
            bot = _make_bot()
            cog = FixerHubCog.__new__(FixerHubCog)
            cog.bot = bot

            top_view = FixerTopView()
            inter = _make_interaction(user_id=100)
            inter.client.get_cog = MagicMock(side_effect=lambda n: cog if n == "FixerHub" else None)
            player_btn = _find_button(top_view, "Player")
            await player_btn.callback(inter)

            sub_view = inter.response.send_message.call_args[1]["view"]
            assert isinstance(sub_view, PlayerSubView)

            inter2 = _make_interaction(user_id=100)
            add_btn = _find_button(sub_view, "Add Item")
            await add_btn.callback(inter2)
            picker_view = inter2.followup.send.call_args[1]["view"]
            assert isinstance(picker_view, PlayerAddItemPickerView)

            target_member = _make_member(300, "TargetPlayer")
            inter3 = _make_interaction(user_id=100)
            user_select = _find_user_select(picker_view)
            user_select._values = [target_member]
            await user_select.callback(inter3)

            inter3.response.edit_message.assert_called_once()
            edit_kwargs = inter3.response.edit_message.call_args[1]
            assert edit_kwargs["view"] is picker_view

            char_select = None
            for child in picker_view.children:
                if isinstance(child, discord.ui.Select) and child.placeholder and "character" in child.placeholder.lower():
                    char_select = child
                    break
            assert char_select is not None, "Character dropdown should be added after user selection"

            inter4 = _make_interaction(user_id=100)
            inter4.data = {"values": ["char-p1"]}
            await char_select.callback(inter4)
            assert picker_view.selected_character is not None
            assert picker_view.selected_character["name"] == "V"

            inter5 = _make_interaction(user_id=100)
            continue_btn = _find_button(picker_view, "Continue →")
            await continue_btn.callback(inter5)

            mock_text.assert_called_once()
            mock_add.assert_called_once()
            add_call_data = mock_add.call_args[0][0]
            assert add_call_data["name"] == "Militech Pistol"
            assert add_call_data["item_type"] == "gun"
            assert add_call_data["owner_id"] == "300"
            assert add_call_data["character_name"] == "V"
            mock_history.assert_called_once()

        _run(_test())


class TestFlowC_TradeFullFlow:
    """Flow C: Player Hub Trade -> select buyer -> item -> char -> price -> DM -> accept."""

    @patch("NightCityBot.cogs.player_hub.ih_record_event", new_callable=AsyncMock)
    @patch("NightCityBot.cogs.player_hub.pt_create", new_callable=AsyncMock)
    @patch("NightCityBot.cogs.player_hub.pi_update_owner", new_callable=AsyncMock, return_value=True)
    @patch("NightCityBot.cogs.player_hub.pi_get_item", new_callable=AsyncMock)
    @patch("NightCityBot.cogs.player_hub.collect_text_input", new_callable=AsyncMock)
    @patch("NightCityBot.cogs.player_hub.get_active_characters", new_callable=AsyncMock)
    @patch("NightCityBot.cogs.player_hub.pi_get_by_owner", new_callable=AsyncMock)
    def test_trade_accept_flow(self, mock_inv, mock_chars, mock_text, mock_get_item,
                               mock_transfer, mock_pt, mock_history):
        mock_inv.return_value = SAMPLE_ITEMS
        mock_chars.return_value = MOCK_CHARS_BUYER
        mock_text.return_value = "1000"
        mock_get_item.return_value = {
            "item_id": "uuid-item-1",
            "owner_id": "100",
            "name": "Katana",
            "item_type": "melee",
            "restriction": "basic",
        }

        async def _test():
            bot = _make_bot()
            cog = PlayerHubCog(bot)

            inv_cog = MagicMock()
            inv_cog.unbelievaboat = MagicMock()
            inv_cog.unbelievaboat.get_balance = AsyncMock(return_value={"cash": 5000, "bank": 5000})
            inv_cog.unbelievaboat.update_balance = AsyncMock(return_value=True)
            bot.cogs["PlayerInventory"] = inv_cog

            ctx = _make_ctx(author_id=100)
            groups = [{"name": "Katana", "count": 1, "items": SAMPLE_ITEMS}]
            trade_view = TradeSetupView(cog, ctx, groups)

            buyer = _make_member(200, "BuyerPlayer")

            inter1 = _make_interaction(user_id=100)
            user_select = _find_user_select(trade_view)
            user_select._values = [buyer]
            await user_select.callback(inter1)

            assert trade_view.selected_buyer is buyer
            char_select = None
            for child in trade_view.children:
                if isinstance(child, discord.ui.Select) and child.placeholder and "buyer" in child.placeholder.lower():
                    char_select = child
                    break
            assert char_select is not None, "Buyer character dropdown must appear"

            inter2 = _make_interaction(user_id=100)
            inter2.data = {"values": ["Johnny"]}
            await trade_view._on_buyer_char_select(inter2)
            assert trade_view.selected_buyer_char_name == "Johnny"

            inter3 = _make_interaction(user_id=100)
            inter3.data = {"values": ["0"]}
            await trade_view._on_item_select(inter3)
            assert trade_view.selected_group_idx == 0

            inter4 = _make_interaction(user_id=100)

            async def mock_wait(self_view):
                self_view.accepted = True

            with patch.object(TradeConfirmView, "wait", mock_wait):
                continue_btn = _find_button(trade_view, "Continue →")
                await continue_btn.callback(inter4)

            mock_text.assert_called_once()
            buyer.send.assert_called_once()
            dm_content = buyer.send.call_args[0][0]
            assert "Katana" in dm_content
            assert "$1,000" in dm_content

            mock_transfer.assert_called_once()
            transfer_args = mock_transfer.call_args[0]
            assert transfer_args[0] == "uuid-item-1"
            assert transfer_args[1] == "200"
            assert transfer_args[2] == "Johnny"

            assert inv_cog.unbelievaboat.update_balance.call_count == 2
            buyer_debit = inv_cog.unbelievaboat.update_balance.call_args_list[0]
            assert buyer_debit[0][0] == 200
            debit_payload = buyer_debit[0][1]
            assert debit_payload.get("cash", 0) < 0 or debit_payload.get("bank", 0) < 0

            seller_credit = inv_cog.unbelievaboat.update_balance.call_args_list[1]
            assert seller_credit[0][0] == 100
            assert seller_credit[0][1] == {"cash": 1000}

        _run(_test())


class TestFlowD_SelfTrade:
    """Flow D: Player Hub Trade -> self-trade blocked."""

    @patch("NightCityBot.cogs.player_hub.get_active_characters", new_callable=AsyncMock, return_value=MOCK_CHARS_BUYER)
    @patch("NightCityBot.cogs.player_hub.pi_get_by_owner", new_callable=AsyncMock, return_value=SAMPLE_ITEMS)
    def test_self_trade_blocked(self, mock_inv, mock_chars):
        async def _test():
            bot = _make_bot()
            cog = PlayerHubCog(bot)
            ctx = _make_ctx(author_id=100)
            groups = [{"name": "Katana", "count": 1, "items": SAMPLE_ITEMS}]

            trade_view = TradeSetupView(cog, ctx, groups)

            self_member = _make_member(100, "SelfPlayer")

            inter1 = _make_interaction(user_id=100)
            user_select = _find_user_select(trade_view)
            user_select._values = [self_member]
            await user_select.callback(inter1)

            trade_view.selected_buyer_char_name = "Johnny"
            trade_view.selected_group_idx = 0

            inter2 = _make_interaction(user_id=100)
            continue_btn = _find_button(trade_view, "Continue →")
            await continue_btn.callback(inter2)

            msg = inter2.response.send_message.call_args[0][0]
            assert "yourself" in msg.lower()

        _run(_test())


class TestFlowE_StaleItem:
    """Flow E: Trade -> stale item (sold between selection and confirmation)."""

    @patch("NightCityBot.cogs.player_hub.ih_record_event", new_callable=AsyncMock)
    @patch("NightCityBot.cogs.player_hub.pt_create", new_callable=AsyncMock)
    @patch("NightCityBot.cogs.player_hub.pi_update_owner", new_callable=AsyncMock)
    @patch("NightCityBot.cogs.player_hub.pi_get_item", new_callable=AsyncMock, return_value=None)
    @patch("NightCityBot.cogs.player_hub.collect_text_input", new_callable=AsyncMock, return_value="500")
    @patch("NightCityBot.cogs.player_hub.get_active_characters", new_callable=AsyncMock, return_value=MOCK_CHARS_BUYER)
    @patch("NightCityBot.cogs.player_hub.pi_get_by_owner", new_callable=AsyncMock, return_value=SAMPLE_ITEMS)
    def test_stale_item_blocked(self, mock_inv, mock_chars, mock_text,
                                mock_get_item, mock_transfer, mock_pt, mock_history):
        async def _test():
            bot = _make_bot()
            cog = PlayerHubCog(bot)
            inv_cog = MagicMock()
            inv_cog.unbelievaboat = MagicMock()
            inv_cog.unbelievaboat.get_balance = AsyncMock(return_value={"cash": 5000, "bank": 5000})
            inv_cog.unbelievaboat.update_balance = AsyncMock(return_value=True)
            bot.cogs["PlayerInventory"] = inv_cog

            ctx = _make_ctx(author_id=100)
            groups = [{"name": "Katana", "count": 1, "items": SAMPLE_ITEMS}]
            trade_view = TradeSetupView(cog, ctx, groups)

            buyer = _make_member(200, "BuyerPlayer")

            inter1 = _make_interaction(user_id=100)
            user_select = _find_user_select(trade_view)
            user_select._values = [buyer]
            await user_select.callback(inter1)

            trade_view.selected_buyer_char_name = "Johnny"
            trade_view.selected_group_idx = 0

            inter2 = _make_interaction(user_id=100)
            async def mock_wait(self_view):
                self_view.accepted = True

            with patch.object(TradeConfirmView, "wait", mock_wait):
                continue_btn = _find_button(trade_view, "Continue →")
                await continue_btn.callback(inter2)

            mock_transfer.assert_not_called()
            inv_cog.unbelievaboat.update_balance.assert_not_called()

            found_stale_msg = False
            for call in inter2.followup.send.call_args_list:
                if call[0] and "no longer" in call[0][0].lower():
                    found_stale_msg = True
                    break
            assert found_stale_msg, "Should inform user the item is no longer available"

        _run(_test())


class TestFlowF_RipperdocSell:
    """Flow F: Ripperdoc Sell -> patient -> stock -> price -> DM -> accept."""

    @patch("NightCityBot.cogs.ripperdoc_hub.ih_record_event", new_callable=AsyncMock)
    @patch("NightCityBot.cogs.ripperdoc_hub.pt_create", new_callable=AsyncMock)
    @patch("NightCityBot.cogs.ripperdoc_hub.pi_add_item", new_callable=AsyncMock, return_value=True)
    @patch("NightCityBot.cogs.ripperdoc_hub.ensure_character_active", new_callable=AsyncMock, return_value=True)
    @patch("NightCityBot.cogs.ripperdoc_hub.collect_text_input", new_callable=AsyncMock, return_value="2000")
    @patch("NightCityBot.cogs.ripperdoc_hub.get_active_characters", new_callable=AsyncMock)
    def test_sell_to_patient_accept(self, mock_chars, mock_text, mock_ensure,
                                    mock_add, mock_pt, mock_history):
        mock_chars.return_value = MOCK_CHARS_PATIENT

        async def _test():
            bot = _make_bot()
            cog = RipperdocHub.__new__(RipperdocHub)
            cog.bot = bot
            cog.unbelievaboat = MagicMock()
            cog.unbelievaboat.get_balance = AsyncMock(return_value={"cash": 10000, "bank": 5000})
            cog.unbelievaboat.update_balance = AsyncMock(return_value=True)

            cw_cog = MagicMock()
            cw_cog._remove_from_inventory = AsyncMock(return_value=True)
            cw_cog._load_inventory = AsyncMock(return_value=[
                {"item_id": "cw-item-1", "item_name": "Kiroshi Optics", "category": "optics"},
            ])
            cw_cog._save_inventory = AsyncMock()
            lock_cm = MagicMock()
            lock_cm.__aenter__ = AsyncMock(return_value=None)
            lock_cm.__aexit__ = AsyncMock(return_value=False)
            cw_cog._locks = MagicMock()
            cw_cog._locks.acquire = MagicMock(return_value=lock_cm)
            bot.cogs["CyberwareShop"] = cw_cog

            ctx = _make_ctx(author_id=100)
            groups = [
                {
                    "name": "Kiroshi Optics",
                    "count": 1,
                    "items": [
                        {
                            "item_id": "cw-item-1",
                            "item_name": "Kiroshi Optics",
                            "category": "optics",
                        }
                    ],
                }
            ]

            sell_view = SellSetupView(cog, ctx, groups, mode="sell")

            patient = _make_member(400, "PatientPlayer")

            inter1 = _make_interaction(user_id=100)
            user_select = _find_user_select(sell_view)
            user_select._values = [patient]
            await user_select.callback(inter1)

            assert sell_view.selected_patient is patient

            char_select = None
            for child in sell_view.children:
                if isinstance(child, discord.ui.Select) and child.placeholder and "character" in child.placeholder.lower():
                    char_select = child
                    break
            assert char_select is not None

            inter2 = _make_interaction(user_id=100)
            inter2.data = {"values": ["char-pat1"]}
            await char_select.callback(inter2)
            assert sell_view.selected_character is not None
            assert sell_view.selected_character["name"] == "Judy"

            stock_select = _find_select(sell_view, "stock")
            inter3 = _make_interaction(user_id=100)
            inter3.data = {"values": ["0"]}
            await stock_select.callback(inter3)
            assert sell_view.selected_group_idx == 0

            inter4 = _make_interaction(user_id=100)

            async def mock_dm_wait(self_view):
                self_view.accepted = True

            with patch.object(DMConfirmView, "wait", mock_dm_wait):
                continue_btn = _find_button(sell_view, "Continue →")
                await continue_btn.callback(inter4)

            mock_text.assert_called_once()
            patient.send.assert_called_once()
            dm_content = patient.send.call_args[0][0]
            assert "Kiroshi Optics" in dm_content
            assert "$2,000" in dm_content

            mock_add.assert_called_once()
            add_data = mock_add.call_args[0][0]
            assert add_data["owner_id"] == "400"
            assert add_data["character_name"] == "Judy"
            assert add_data["name"] == "Kiroshi Optics"

            assert cog.unbelievaboat.update_balance.call_count == 2
            patient_debit = cog.unbelievaboat.update_balance.call_args_list[0]
            assert patient_debit[0][0] == 400

        _run(_test())


class TestFlowG_GunstoreRestrictedSell:
    """Flow G: Gunstore sell controlled gun -> approval -> DM -> accept."""

    @patch("NightCityBot.cogs.gunstore_hub.ih_record_event", new_callable=AsyncMock)
    @patch("NightCityBot.cogs.gunstore_hub.pt_create", new_callable=AsyncMock)
    @patch("NightCityBot.cogs.gunstore_hub.pi_add_item", new_callable=AsyncMock, return_value=True)
    @patch("NightCityBot.cogs.gunstore_hub.ensure_character_active", new_callable=AsyncMock, return_value=True)
    @patch("NightCityBot.cogs.gunstore_hub.collect_text_input", new_callable=AsyncMock, return_value="3000")
    @patch("NightCityBot.cogs.gunstore_hub.get_active_characters", new_callable=AsyncMock)
    def test_controlled_gun_sell(self, mock_chars, mock_text, mock_ensure,
                                 mock_add, mock_pt, mock_history):
        mock_chars.return_value = MOCK_CHARS_CUSTOMER

        async def _test():
            bot = _make_bot()
            cog = GunstoreHub.__new__(GunstoreHub)
            cog.bot = bot
            cog.unbelievaboat = MagicMock()
            cog.unbelievaboat.get_balance = AsyncMock(return_value={"cash": 10000, "bank": 5000})
            cog.unbelievaboat.update_balance = AsyncMock(return_value=True)

            guns_cog = MagicMock()
            store_lots_copy = [
                {
                    "gun_name": "Tsunami Nekomata",
                    "restriction": "controlled",
                    "qty_remaining": 2,
                    "lot_id": "lot-1",
                    "unit_cost": 3000,
                    "item_ids": ["gun-uuid-1", "gun-uuid-2"],
                }
            ]
            guns_cog._load_state = AsyncMock(return_value={
                "stores": {
                    "store-1": {
                        "lots": store_lots_copy,
                        "controlled_buyers": [],
                    }
                }
            })
            guns_cog._save_state = AsyncMock()
            guns_cog._remove_from_store = AsyncMock(return_value=True)
            lock_cm = MagicMock()
            lock_cm.__aenter__ = AsyncMock(return_value=None)
            lock_cm.__aexit__ = AsyncMock(return_value=False)
            guns_cog.lock = lock_cm
            bot.cogs["GunsShopCog"] = guns_cog

            def mock_guns_cog_fn():
                return guns_cog
            cog._guns_cog = mock_guns_cog_fn

            ctx = _make_ctx(author_id=100)
            lots = [
                {
                    "gun_name": "Tsunami Nekomata",
                    "restriction": "controlled",
                    "qty_remaining": 2,
                    "lot_id": "lot-1",
                    "unit_cost": 3000,
                }
            ]

            sell_view = GunSellSetupView(cog, ctx, lots, "store-1")

            customer = _make_member(500, "CustomerPlayer")

            inter1 = _make_interaction(user_id=100)
            user_select = _find_user_select(sell_view)
            user_select._values = [customer]
            await user_select.callback(inter1)

            assert sell_view.selected_customer is customer

            char_select = None
            for child in sell_view.children:
                if isinstance(child, discord.ui.Select) and child.placeholder and "character" in child.placeholder.lower():
                    char_select = child
                    break
            assert char_select is not None

            inter2 = _make_interaction(user_id=100)
            inter2.data = {"values": ["char-cust1"]}
            await char_select.callback(inter2)
            assert sell_view.selected_character["name"] == "Panam"

            stock_select = _find_select(sell_view, "stock")
            inter3 = _make_interaction(user_id=100)
            inter3.data = {"values": ["0"]}
            await stock_select.callback(inter3)
            assert sell_view.selected_lot_idx == 0

            inter4 = _make_interaction(user_id=100)

            async def mock_approve_wait(self_view):
                self_view.approved = True

            async def mock_dm_wait(self_view):
                self_view.accepted = True

            with patch.object(InlineApproveView, "wait", mock_approve_wait), \
                 patch.object(GunDMConfirmView, "wait", mock_dm_wait):
                continue_btn = _find_button(sell_view, "Continue →")
                await continue_btn.callback(inter4)

            mock_text.assert_called_once()
            customer.send.assert_called_once()
            dm_content = customer.send.call_args[0][0]
            assert "Tsunami Nekomata" in dm_content

            mock_add.assert_called_once()
            add_data = mock_add.call_args[0][0]
            assert add_data["owner_id"] == "500"
            assert add_data["character_name"] == "Panam"
            assert add_data["name"] == "Tsunami Nekomata"

        _run(_test())


class TestWholesaleClearInteractionCheck:
    """Verify WholesaleClearConfirmView now has interaction_check."""

    def test_wrong_user_blocked(self):
        async def _test():
            bot = _make_bot()
            cog = AdminShopCog.__new__(AdminShopCog)
            cog.bot = bot
            ctx = _make_ctx(author_id=100)
            view = WholesaleClearConfirmView(cog, ctx, target="guns")

            inter_wrong = _make_interaction(user_id=999)
            result = await view.interaction_check(inter_wrong)
            assert result is False
        _run(_test())

    def test_correct_user_allowed(self):
        async def _test():
            bot = _make_bot()
            cog = AdminShopCog.__new__(AdminShopCog)
            cog.bot = bot
            ctx = _make_ctx(author_id=100)
            view = WholesaleClearConfirmView(cog, ctx, target="guns")

            inter_ok = _make_interaction(user_id=100)
            result = await view.interaction_check(inter_ok)
            assert result is True
        _run(_test())


class TestTradeConfirmInteractionCheck:
    """Verify TradeConfirmView only allows the recipient."""

    def test_wrong_user_rejected(self):
        async def _test():
            view = TradeConfirmView(recipient_id=200, timeout=60)
            inter = _make_interaction(user_id=999)
            result = await view.interaction_check(inter)
            assert result is False
        _run(_test())

    def test_correct_user_accepted(self):
        async def _test():
            view = TradeConfirmView(recipient_id=200, timeout=60)
            inter = _make_interaction(user_id=200)
            result = await view.interaction_check(inter)
            assert result is True
        _run(_test())


class TestDMConfirmInteractionCheck:
    """Verify DMConfirmView only allows the recipient."""

    def test_wrong_user_rejected(self):
        async def _test():
            view = DMConfirmView(recipient_id=400, timeout=60)
            inter = _make_interaction(user_id=999)
            result = await view.interaction_check(inter)
            assert result is False
        _run(_test())

    def test_correct_user_accepted(self):
        async def _test():
            view = DMConfirmView(recipient_id=400, timeout=60)
            inter = _make_interaction(user_id=400)
            result = await view.interaction_check(inter)
            assert result is True
        _run(_test())


class TestGunDMConfirmInteractionCheck:
    """Verify GunDMConfirmView only allows the recipient."""

    def test_wrong_user_rejected(self):
        async def _test():
            view = GunDMConfirmView(recipient_id=500, timeout=60)
            inter = _make_interaction(user_id=999)
            result = await view.interaction_check(inter)
            assert result is False
        _run(_test())

    def test_correct_user_accepted(self):
        async def _test():
            view = GunDMConfirmView(recipient_id=500, timeout=60)
            inter = _make_interaction(user_id=500)
            result = await view.interaction_check(inter)
            assert result is True
        _run(_test())
