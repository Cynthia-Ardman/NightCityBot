"""Tests for the PlayerInventoryCog — my_inventory, trade, inv_give, inv_add/remove/reassign."""

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from NightCityBot.cogs.player_inventory import PlayerInventoryCog, TradeConfirmView


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _run(coro):
    return asyncio.run(coro)


def _make_cog(monkeypatch):
    """Build a PlayerInventoryCog with all DB calls and channel lookups mocked out."""
    monkeypatch.setattr("config.FIXER_ROLE_ID", 900)
    monkeypatch.setattr("config.RIPPERDOC_ROLE_ID", 800)
    monkeypatch.setattr("config.GEAR_MISC_LOG_CHANNEL_ID", 0)
    monkeypatch.setattr("config.GUN_LOG_CHANNEL_ID", 0)
    monkeypatch.setattr("config.CYBERWARE_LOG_CHANNEL_ID", 0)
    monkeypatch.setattr("config.NIGHTCITYBOT_LOG_CHANNEL_ID", 0)
    monkeypatch.setattr("NightCityBot.cogs.player_inventory.ih_record_event", AsyncMock())

    bot = MagicMock()
    bot.get_channel = MagicMock(return_value=None)
    bot.fetch_channel = AsyncMock(side_effect=Exception("no channel"))
    bot.cogs = {}

    ub = MagicMock()
    ub.get_balance = AsyncMock(return_value={"cash": 0, "bank": 0})
    ub.update_balance = AsyncMock(return_value=True)

    cog = PlayerInventoryCog.__new__(PlayerInventoryCog)
    cog.bot = bot
    cog.unbelievaboat = ub
    return cog


def _ctx(author_id=111, guild=True):
    ctx = MagicMock()
    ctx.send = AsyncMock()
    ctx.author = MagicMock(spec=discord.Member)
    ctx.author.id = author_id
    ctx.author.display_name = f"User{author_id}"
    ctx.author.mention = f"<@{author_id}>"
    ctx.author.roles = []
    ctx.author.guild_permissions = MagicMock()
    ctx.author.guild_permissions.administrator = False
    if guild:
        ctx.guild = MagicMock()
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


def _auto_accept_trade_view(monkeypatch):
    """Patch TradeConfirmView so it auto-accepts immediately (no 60s wait)."""
    _orig_init = TradeConfirmView.__init__

    def _patched_init(self, timeout=60):
        _orig_init(self, timeout=timeout)
        self.accepted = True

    monkeypatch.setattr(TradeConfirmView, "__init__", _patched_init)
    monkeypatch.setattr(TradeConfirmView, "wait", AsyncMock(return_value=None))


def _item(name="Kiroshi", item_type="cyberware", restriction="basic",
          char="V", price=3000, seller="Doc", date="2026-04-01"):
    return {
        "item_id": str(uuid.uuid4()),
        "name": name,
        "item_type": item_type,
        "restriction": restriction,
        "character_name": char,
        "price_paid": price,
        "seller_name": seller,
        "acquired_at": date,
        "owner_id": "111",
    }


async def _cmd(cog, method_name, ctx, *args, **kwargs):
    cmd = getattr(cog, method_name)
    if hasattr(cmd, "callback"):
        return await cmd.callback(cog, ctx, *args, **kwargs)
    return await cmd(ctx, *args, **kwargs)


# ------------------------------------------------------------------
# TestGroupItems — static helper
# ------------------------------------------------------------------

class TestGroupItems:
    def test_identical_items_grouped_together(self):
        items = [
            _item("Kiroshi", date="2026-04-01"),
            _item("Kiroshi", date="2026-04-01"),
        ]
        groups = PlayerInventoryCog._group_items(items)
        assert len(groups) == 1
        assert groups[0]["count"] == 2

    def test_different_names_separate_groups(self):
        items = [_item("Sandevistan"), _item("Kiroshi")]
        groups = PlayerInventoryCog._group_items(items)
        # Alphabetical: Kiroshi, Sandevistan
        assert groups[0]["name"] == "Kiroshi"
        assert groups[1]["name"] == "Sandevistan"

    def test_fifo_ordering_within_group(self):
        items = [
            _item("Kiroshi", date="2026-04-01"),
            _item("Kiroshi", date="2026-04-01"),
        ]
        # Two items on the same date stay in one group, FIFO by acquired_at
        groups = PlayerInventoryCog._group_items(items)
        assert len(groups) == 1
        assert groups[0]["count"] == 2
        assert groups[0]["items"][0]["acquired_at"] == "2026-04-01"

    def test_different_dates_produce_separate_groups(self):
        """Items with same name/price/seller but different acquisition dates → separate rows."""
        items = [
            _item("Kiroshi", date="2026-04-02"),
            _item("Kiroshi", date="2026-04-01"),
        ]
        groups = PlayerInventoryCog._group_items(items)
        # Each distinct date is its own group
        assert len(groups) == 2
        # Sorted by (name, acquired_date): 2026-04-01 first
        assert groups[0]["acquired_date"] == "2026-04-01"
        assert groups[1]["acquired_date"] == "2026-04-02"
        assert groups[0]["count"] == 1
        assert groups[1]["count"] == 1

    def test_acquired_date_in_group_key(self):
        """acquired_date is included in each group dict for display."""
        items = [_item("Sandevistan", date="2026-03-15")]
        groups = PlayerInventoryCog._group_items(items)
        assert groups[0]["acquired_date"] == "2026-03-15"


# ------------------------------------------------------------------
# TestBuildDisplay — grouped character display
# ------------------------------------------------------------------

class TestBuildDisplay:
    def test_groups_by_character(self):
        items = [
            {**_item("Kiroshi", char="V"),     "owner_id": "1"},
            {**_item("Berserk", char="Johnny"), "owner_id": "1"},
        ]
        display, groups = PlayerInventoryCog._build_display(items)
        # Two character headers + two item rows
        header_lines = [ln for rn, ln in display if rn is None]
        assert any("V" in h for h in header_lines)
        assert any("Johnny" in h for h in header_lines)

    def test_row_numbers_sequential(self):
        items = [
            {**_item("Kiroshi", char="V"),     "owner_id": "1"},
            {**_item("Berserk", char="Johnny"), "owner_id": "1"},
        ]
        display, groups = PlayerInventoryCog._build_display(items)
        item_rows = [rn for rn, _ in display if rn is not None]
        assert item_rows == [1, 2]

    def test_char_filter_narrows_display(self):
        items = [
            {**_item("Kiroshi", char="V"),      "owner_id": "1"},
            {**_item("Berserk",  char="Johnny"), "owner_id": "1"},
        ]
        display, groups = PlayerInventoryCog._build_display(items, char_filter="V")
        assert all(
            rn is None or "Kiroshi" in ln or "V" in ln
            for rn, ln in display
        )
        item_rows = [rn for rn, _ in display if rn is not None]
        assert len(item_rows) == 1

    def test_char_filter_preserves_global_row_numbers(self):
        """Filtered view shows GLOBAL row numbers matching !trade / !inv_give row resolution."""
        items = [
            {**_item("Axe",     char="Alpha"), "owner_id": "1"},  # global row 1
            {**_item("Bomb",    char="Alpha"), "owner_id": "1"},  # global row 2
            {**_item("Pistol",  char="V"),     "owner_id": "1"},  # global row 3
        ]
        # Without filter: rows 1, 2, 3
        display_all, _ = PlayerInventoryCog._build_display(items)
        all_rows = [rn for rn, _ in display_all if rn is not None]
        assert all_rows == [1, 2, 3]

        # With filter "V": only V's item is shown, but its row number stays 3
        display_v, groups_v = PlayerInventoryCog._build_display(items, char_filter="V")
        filtered_rows = [rn for rn, _ in display_v if rn is not None]
        assert filtered_rows == [3]   # NOT [1] — global position preserved
        assert len(groups_v) == 1
        assert groups_v[0]["name"] == "Pistol"


# ------------------------------------------------------------------
# TestMyInventory
# ------------------------------------------------------------------

class TestMyInventory:
    def test_dm_guard(self, monkeypatch):
        cog = _make_cog(monkeypatch)
        monkeypatch.setattr("NightCityBot.cogs.player_inventory.pi_get_by_owner", AsyncMock(return_value=[]))
        ctx = _ctx(guild=False)
        _run(_cmd(cog, "my_inventory", ctx))
        assert "server" in ctx.send.call_args[0][0]

    def test_empty_inventory(self, monkeypatch):
        cog = _make_cog(monkeypatch)
        monkeypatch.setattr("NightCityBot.cogs.player_inventory.pi_get_by_owner", AsyncMock(return_value=[]))
        ctx = _ctx()
        _run(_cmd(cog, "my_inventory", ctx))
        assert "empty" in ctx.send.call_args[0][0]

    def test_grouped_display_shows_embed(self, monkeypatch):
        cog = _make_cog(monkeypatch)
        items = [_item("Kiroshi", char="V"), _item("Berserk", char="Johnny")]
        monkeypatch.setattr("NightCityBot.cogs.player_inventory.pi_get_by_owner", AsyncMock(return_value=items))
        ctx = _ctx()
        _run(_cmd(cog, "my_inventory", ctx))
        call_kwargs = ctx.send.call_args[1]
        assert "embed" in call_kwargs
        embed = call_kwargs["embed"]
        assert "Kiroshi" in embed.description or "Berserk" in embed.description

    def test_char_filter_via_query(self, monkeypatch):
        cog = _make_cog(monkeypatch)
        items = [_item("Kiroshi", char="V"), _item("Berserk", char="Johnny")]
        monkeypatch.setattr("NightCityBot.cogs.player_inventory.pi_get_by_owner", AsyncMock(return_value=items))
        ctx = _ctx()
        _run(_cmd(cog, "my_inventory", ctx, query="V"))
        embed = ctx.send.call_args[1]["embed"]
        assert "Kiroshi" in embed.description
        assert "Berserk" not in embed.description

    def test_non_fixer_cannot_view_other_player(self, monkeypatch):
        cog = _make_cog(monkeypatch)
        monkeypatch.setattr("NightCityBot.cogs.player_inventory.pi_get_by_owner", AsyncMock(return_value=[]))
        ctx = _ctx(author_id=111)
        # Simulate a mention in query — parse the member differently
        # We test via direct call with a query that would be parsed as a member mention
        # Since we can't easily mock Member conversion in query parsing, test via the privileged check:
        other = _make_member(999)
        # Directly test the privilege check (invoked when target != ctx.author)
        ctx.author.roles = []  # no fixer role
        # Patch guild.get_member to return other
        ctx.guild.get_member = MagicMock(return_value=other)
        _run(_cmd(cog, "my_inventory", ctx, query=f"<@{other.id}>"))
        # Should be rejected since author has no fixer role
        assert "Fixers or admins" in ctx.send.call_args[0][0]


# ------------------------------------------------------------------
# TestTrade
# ------------------------------------------------------------------

class TestTrade:
    def test_self_trade_allowed(self, monkeypatch):
        """Price=0 self-trade is valid — moves item between own characters."""
        cog = _make_cog(monkeypatch)
        item = _item("Kiroshi")
        monkeypatch.setattr("NightCityBot.cogs.player_inventory.pi_get_by_owner", AsyncMock(return_value=[item]))
        monkeypatch.setattr("NightCityBot.cogs.player_inventory.pi_get_item", AsyncMock(return_value=item))
        monkeypatch.setattr("NightCityBot.cogs.player_inventory.pi_update_owner", AsyncMock(return_value=True))
        ctx = _ctx(author_id=111)
        buyer = _make_member(111)  # same user
        _run(_cmd(cog, "trade", ctx, buyer, 1, 0, buyer_character="Johnny"))
        assert "✅" in ctx.send.call_args[0][0]

    def test_negative_price_rejected(self, monkeypatch):
        cog = _make_cog(monkeypatch)
        item = _item("Kiroshi")
        monkeypatch.setattr("NightCityBot.cogs.player_inventory.pi_get_by_owner", AsyncMock(return_value=[item]))
        ctx = _ctx(author_id=111)
        _run(_cmd(cog, "trade", ctx, _make_member(999), 1, -100, buyer_character="V"))
        assert "negative" in ctx.send.call_args[0][0]

    def test_controlled_gun_blocked(self, monkeypatch):
        cog = _make_cog(monkeypatch)
        item = _item("Liberty", item_type="gun", restriction="controlled")
        monkeypatch.setattr("NightCityBot.cogs.player_inventory.pi_get_by_owner", AsyncMock(return_value=[item]))
        monkeypatch.setattr("NightCityBot.cogs.player_inventory.pi_get_item", AsyncMock(return_value=item))
        ctx = _ctx(author_id=111)
        _run(_cmd(cog, "trade", ctx, _make_member(999), 1, 2000, buyer_character="V"))
        msg = ctx.send.call_args[0][0]
        assert "controlled" in msg

    def test_restricted_gun_blocked(self, monkeypatch):
        cog = _make_cog(monkeypatch)
        item = _item("Militech", item_type="gun", restriction="restricted")
        monkeypatch.setattr("NightCityBot.cogs.player_inventory.pi_get_by_owner", AsyncMock(return_value=[item]))
        monkeypatch.setattr("NightCityBot.cogs.player_inventory.pi_get_item", AsyncMock(return_value=item))
        ctx = _ctx(author_id=111)
        _run(_cmd(cog, "trade", ctx, _make_member(999), 1, 5000, buyer_character="V"))
        assert "restricted" in ctx.send.call_args[0][0]

    def test_invalid_row_rejected(self, monkeypatch):
        cog = _make_cog(monkeypatch)
        monkeypatch.setattr("NightCityBot.cogs.player_inventory.pi_get_by_owner", AsyncMock(return_value=[]))
        ctx = _ctx(author_id=111)
        _run(_cmd(cog, "trade", ctx, _make_member(999), 5, 1000, buyer_character="V"))
        assert "Invalid row" in ctx.send.call_args[0][0]

    def test_buyer_cannot_afford(self, monkeypatch):
        _auto_accept_trade_view(monkeypatch)
        cog = _make_cog(monkeypatch)
        item = _item("Kiroshi")
        monkeypatch.setattr("NightCityBot.cogs.player_inventory.pi_get_by_owner", AsyncMock(return_value=[item]))
        monkeypatch.setattr("NightCityBot.cogs.player_inventory.pi_get_item", AsyncMock(return_value=item))
        cog.unbelievaboat.get_balance = AsyncMock(return_value={"cash": 100, "bank": 0})
        ctx = _ctx(author_id=111)
        _run(_cmd(cog, "trade", ctx, _make_member(999), 1, 5000, buyer_character="V"))
        assert "cannot afford" in ctx.send.call_args[0][0]

    def test_db_failure_creates_pending_transfer(self, monkeypatch):
        """If seller credit fails after buyer debit, a pending_transfers record is created."""
        _auto_accept_trade_view(monkeypatch)
        cog = _make_cog(monkeypatch)
        item = _item("Kiroshi", item_type="cyberware")
        monkeypatch.setattr("NightCityBot.cogs.player_inventory.pi_get_by_owner", AsyncMock(return_value=[item]))
        monkeypatch.setattr("NightCityBot.cogs.player_inventory.pi_get_item", AsyncMock(return_value=item))

        pt_records = []

        async def capture_pt(rec):
            pt_records.append(rec)
            return True

        monkeypatch.setattr("NightCityBot.cogs.player_inventory.pt_create", capture_pt)

        call_count = 0

        async def fail_on_second(user_id, payload, **kwargs):
            nonlocal call_count
            call_count += 1
            return call_count != 2  # fail only second call (seller credit)

        cog.unbelievaboat.get_balance = AsyncMock(return_value={"cash": 10000, "bank": 0})
        cog.unbelievaboat.update_balance = AsyncMock(side_effect=fail_on_second)

        ctx = _ctx(author_id=111)
        _run(_cmd(cog, "trade", ctx, _make_member(999), 1, 2000, buyer_character="V"))

        assert len(pt_records) == 1
        assert pt_records[0]["amount"] == 2000
        assert pt_records[0]["seller_id"] == "111"
        assert pt_records[0]["buyer_id"] == "999"
        assert "⚠️" in ctx.send.call_args[0][0]

    def test_stale_item_blocked_by_re_verify(self, monkeypatch):
        """If the item is no longer owned by seller at re-verify, trade is rejected."""
        cog = _make_cog(monkeypatch)
        item = _item("Kiroshi")
        monkeypatch.setattr("NightCityBot.cogs.player_inventory.pi_get_by_owner", AsyncMock(return_value=[item]))
        # pi_get_item returns None (item no longer exists / was already transferred)
        monkeypatch.setattr("NightCityBot.cogs.player_inventory.pi_get_item", AsyncMock(return_value=None))
        ctx = _ctx(author_id=111)
        _run(_cmd(cog, "trade", ctx, _make_member(999), 1, 1000, buyer_character="V"))
        assert "no longer in your inventory" in ctx.send.call_args[0][0]

    def test_success_logs_to_correct_channel(self, monkeypatch):
        """Gun item trade logs to gun-log channel."""
        _auto_accept_trade_view(monkeypatch)
        cog = _make_cog(monkeypatch)
        item = _item("Liberty", item_type="gun")
        monkeypatch.setattr("NightCityBot.cogs.player_inventory.pi_get_by_owner", AsyncMock(return_value=[item]))
        monkeypatch.setattr("NightCityBot.cogs.player_inventory.pi_get_item", AsyncMock(return_value=item))
        monkeypatch.setattr("NightCityBot.cogs.player_inventory.pi_update_owner", AsyncMock(return_value=True))

        gun_ch = MagicMock()
        gun_ch.send = AsyncMock()
        monkeypatch.setattr("config.GUN_LOG_CHANNEL_ID", 555)
        cog.bot.get_channel = MagicMock(return_value=gun_ch)

        ctx = _ctx(author_id=111)
        cog.unbelievaboat.get_balance = AsyncMock(return_value={"cash": 5000, "bank": 0})
        _run(_cmd(cog, "trade", ctx, _make_member(999), 1, 2000, buyer_character="V"))

        assert gun_ch.send.call_count == 1
        assert "✅" in ctx.send.call_args[0][0]


# ------------------------------------------------------------------
# TestInvGive
# ------------------------------------------------------------------

class TestInvGive:
    def test_self_give_allowed(self, monkeypatch):
        """Giving an item to yourself (different character) is valid."""
        cog = _make_cog(monkeypatch)
        item = _item("Kiroshi", char="V")
        monkeypatch.setattr("NightCityBot.cogs.player_inventory.pi_get_by_owner", AsyncMock(return_value=[item]))
        monkeypatch.setattr("NightCityBot.cogs.player_inventory.pi_update_owner", AsyncMock(return_value=True))
        ctx = _ctx(author_id=111)
        target = _make_member(111)  # same user
        _run(_cmd(cog, "inv_give", ctx, target, 1, "V", "Johnny"))
        assert "✅" in ctx.send.call_args[0][0]

    def test_receiver_char_required_for_player_to_player(self, monkeypatch):
        """Player-to-player gives must include a receiver character name."""
        cog = _make_cog(monkeypatch)
        item = _item("Kiroshi", char="V")
        monkeypatch.setattr("NightCityBot.cogs.player_inventory.pi_get_by_owner", AsyncMock(return_value=[item]))
        ctx = _ctx(author_id=111)
        # No receiver_char provided — omit the argument entirely
        _run(_cmd(cog, "inv_give", ctx, _make_member(999), 1, "V"))
        assert "required" in ctx.send.call_args[0][0].lower()

    def test_invalid_row_rejected(self, monkeypatch):
        cog = _make_cog(monkeypatch)
        monkeypatch.setattr("NightCityBot.cogs.player_inventory.pi_get_by_owner", AsyncMock(return_value=[]))
        ctx = _ctx(author_id=111)
        _run(_cmd(cog, "inv_give", ctx, _make_member(999), 5, "V", "Johnny"))
        assert "Invalid row" in ctx.send.call_args[0][0]

    def test_character_mismatch_rejected(self, monkeypatch):
        cog = _make_cog(monkeypatch)
        item = _item("Kiroshi", char="V")
        monkeypatch.setattr("NightCityBot.cogs.player_inventory.pi_get_by_owner", AsyncMock(return_value=[item]))
        ctx = _ctx(author_id=111)
        _run(_cmd(cog, "inv_give", ctx, _make_member(999), 1, "Johnny", "Blade"))
        msg = ctx.send.call_args[0][0]
        assert "belongs to character" in msg

    def test_cyberware_to_ripperdoc_goes_to_cw_stock(self, monkeypatch):
        """Cyberware given to a ripperdoc goes into CW stock file, not player_inventory."""
        cog = _make_cog(monkeypatch)
        item = {**_item("Kiroshi", item_type="cyberware", char="V"), "owner_id": "111"}
        monkeypatch.setattr("NightCityBot.cogs.player_inventory.pi_get_by_owner", AsyncMock(return_value=[item]))

        deleted_ids = []

        async def capture_delete(item_id):
            deleted_ids.append(item_id)
            return True

        monkeypatch.setattr("NightCityBot.cogs.player_inventory.pi_delete_item", capture_delete)

        cw_inventory = []

        async def mock_load_inv(uid):
            return list(cw_inventory)

        async def mock_save_inv(uid, inv):
            cw_inventory.clear()
            cw_inventory.extend(inv)
            return True

        cw_cog = MagicMock()
        cw_cog._load_inventory = mock_load_inv
        cw_cog._save_inventory = mock_save_inv
        cog.bot.cogs = {"CyberwareShop": cw_cog}

        ripperdoc_role = MagicMock()
        ripperdoc_role.id = 800  # RIPPERDOC_ROLE_ID
        target = _make_member(500, roles=[ripperdoc_role])

        ctx = _ctx(author_id=111)
        _run(_cmd(cog, "inv_give", ctx, target, 1, "V"))

        # Item removed from player_inventory
        assert len(deleted_ids) == 1
        # Item added to ripperdoc CW stock
        assert len(cw_inventory) == 1
        assert cw_inventory[0]["name"] == "Kiroshi"
        assert "✅" in ctx.send.call_args[0][0]

    def test_cw_stock_save_failure_restores_item(self, monkeypatch):
        """If _save_inventory fails, the deleted item is re-inserted and the user sees an error."""
        cog = _make_cog(monkeypatch)
        item = {**_item("Kiroshi", item_type="cyberware", char="V"), "owner_id": "111"}
        monkeypatch.setattr("NightCityBot.cogs.player_inventory.pi_get_by_owner", AsyncMock(return_value=[item]))

        deleted_ids = []

        async def capture_delete(item_id):
            deleted_ids.append(item_id)
            return True

        monkeypatch.setattr("NightCityBot.cogs.player_inventory.pi_delete_item", capture_delete)

        restored_items = []

        async def capture_add(item_dict):
            restored_items.append(item_dict)
            return True

        monkeypatch.setattr("NightCityBot.cogs.player_inventory.pi_add_item", capture_add)

        async def mock_load_inv(uid):
            return []

        async def mock_save_inv(uid, inv):
            return False  # simulate file write failure

        cw_cog = MagicMock()
        cw_cog._load_inventory = mock_load_inv
        cw_cog._save_inventory = mock_save_inv
        cog.bot.cogs = {"CyberwareShop": cw_cog}

        ripperdoc_role = MagicMock()
        ripperdoc_role.id = 800
        target = _make_member(500, roles=[ripperdoc_role])

        ctx = _ctx(author_id=111)
        _run(_cmd(cog, "inv_give", ctx, target, 1, "V"))

        # Item was deleted from DB
        assert len(deleted_ids) == 1
        # Item was restored to DB
        assert len(restored_items) == 1
        assert restored_items[0]["name"] == "Kiroshi"
        assert restored_items[0]["owner_id"] == "111"
        # User sees error
        msg = ctx.send.call_args[0][0]
        assert "❌" in msg
        assert "restored" in msg.lower() or "try again" in msg.lower()

    def test_cyberware_to_ripperdoc_purchased_at_defaults_to_now(self, monkeypatch):
        """If acquired_at and created_at are both None, purchased_at is set to a non-None value."""
        cog = _make_cog(monkeypatch)
        item = {
            **_item("Kiroshi", item_type="cyberware", char="V"),
            "owner_id": "111",
            "acquired_at": None,
            "created_at": None,
        }
        monkeypatch.setattr("NightCityBot.cogs.player_inventory.pi_get_by_owner", AsyncMock(return_value=[item]))
        monkeypatch.setattr("NightCityBot.cogs.player_inventory.pi_delete_item", AsyncMock(return_value=True))

        cw_inventory = []

        async def mock_load_inv(uid):
            return list(cw_inventory)

        async def mock_save_inv(uid, inv):
            cw_inventory.clear()
            cw_inventory.extend(inv)
            return True

        cw_cog = MagicMock()
        cw_cog._load_inventory = mock_load_inv
        cw_cog._save_inventory = mock_save_inv
        cog.bot.cogs = {"CyberwareShop": cw_cog}

        ripperdoc_role = MagicMock()
        ripperdoc_role.id = 800
        target = _make_member(500, roles=[ripperdoc_role])

        ctx = _ctx(author_id=111)
        _run(_cmd(cog, "inv_give", ctx, target, 1, "V"))

        assert len(cw_inventory) == 1
        # purchased_at must not be None — fallback to datetime.now()
        assert cw_inventory[0]["purchased_at"] is not None

    def test_gun_give_logs_to_gun_channel(self, monkeypatch):
        """Gun item give logs to #gun-log, not #gear-misc-logs."""
        cog = _make_cog(monkeypatch)
        item = {**_item("Liberty", item_type="gun"), "owner_id": "111"}
        monkeypatch.setattr("NightCityBot.cogs.player_inventory.pi_get_by_owner", AsyncMock(return_value=[item]))
        monkeypatch.setattr("NightCityBot.cogs.player_inventory.pi_update_owner", AsyncMock(return_value=True))

        gun_ch = MagicMock()
        gun_ch.send = AsyncMock()
        monkeypatch.setattr("config.GUN_LOG_CHANNEL_ID", 555)
        cog.bot.get_channel = MagicMock(return_value=gun_ch)

        ctx = _ctx(author_id=111)
        _run(_cmd(cog, "inv_give", ctx, _make_member(999), 1, "V", "Johnny"))

        assert gun_ch.send.call_count == 1


# ------------------------------------------------------------------
# TestInvAdd
# ------------------------------------------------------------------

class TestInvAdd:
    _CHAR_RECORD = {"character_id": "char-1", "name": "V", "character_name": "V", "status": "active"}

    def test_qty_creates_multiple_rows(self, monkeypatch):
        cog = _make_cog(monkeypatch)
        added_items = []

        async def capture_add(item_dict):
            added_items.append(item_dict)
            return True

        monkeypatch.setattr("NightCityBot.cogs.player_inventory.pi_add_item", capture_add)
        monkeypatch.setattr("NightCityBot.cogs.player_inventory.get_character_by_name", AsyncMock(return_value=self._CHAR_RECORD))
        monkeypatch.setattr("NightCityBot.cogs.player_inventory.ensure_character_active", AsyncMock(return_value=True))

        ctx = _ctx(author_id=900)
        ctx.author.roles = [MagicMock(id=900)]  # fixer role

        player = _make_member(999)

        async def run():
            cmd = getattr(cog, "inv_add")
            return await cmd.callback(cog, ctx, player, "Trauma Team Card", 3, "V", "gear", "basic", "", None)

        _run(run())

        # 3 separate rows with unique UUIDs
        assert len(added_items) == 3
        item_ids = [d["item_id"] for d in added_items]
        assert len(set(item_ids)) == 3  # all unique UUIDs
        assert all(d["name"] == "Trauma Team Card" for d in added_items)
        assert all(d["character_name"] == "V" for d in added_items)
        assert all(d["restriction"] == "basic" for d in added_items)
        assert "✅" in ctx.send.call_args[0][0]

    def test_qty_zero_rejected(self, monkeypatch):
        cog = _make_cog(monkeypatch)
        monkeypatch.setattr("NightCityBot.cogs.player_inventory.pi_add_item", AsyncMock(return_value=True))
        ctx = _ctx(author_id=900)
        player = _make_member(999)

        async def run():
            cmd = getattr(cog, "inv_add")
            return await cmd.callback(cog, ctx, player, "Card", 0, "V")

        _run(run())
        assert "qty" in ctx.send.call_args[0][0].lower()

    def test_gun_with_controlled_restriction(self, monkeypatch):
        """!inv_add with item_type=gun and restriction=controlled stores correctly."""
        cog = _make_cog(monkeypatch)
        added_items = []

        async def capture_add(item_dict):
            added_items.append(item_dict)
            return True

        monkeypatch.setattr("NightCityBot.cogs.player_inventory.pi_add_item", capture_add)
        monkeypatch.setattr("NightCityBot.cogs.player_inventory.get_character_by_name", AsyncMock(return_value=self._CHAR_RECORD))
        monkeypatch.setattr("NightCityBot.cogs.player_inventory.ensure_character_active", AsyncMock(return_value=True))

        ctx = _ctx(author_id=900)
        player = _make_member(999)

        async def run():
            cmd = getattr(cog, "inv_add")
            return await cmd.callback(
                cog, ctx, player, "Militech M10AF", 1, "V", "gun", "controlled"
            )

        _run(run())

        assert len(added_items) == 1
        assert added_items[0]["item_type"] == "gun"
        assert added_items[0]["restriction"] == "controlled"
        assert "✅" in ctx.send.call_args[0][0]
        assert "[controlled]" in ctx.send.call_args[0][0]

    def test_cyberware_with_basic_restriction(self, monkeypatch):
        """!inv_add with item_type=cyberware stores the item correctly."""
        cog = _make_cog(monkeypatch)
        added_items = []

        async def capture_add(item_dict):
            added_items.append(item_dict)
            return True

        monkeypatch.setattr("NightCityBot.cogs.player_inventory.pi_add_item", capture_add)
        monkeypatch.setattr("NightCityBot.cogs.player_inventory.get_character_by_name", AsyncMock(return_value=self._CHAR_RECORD))
        monkeypatch.setattr("NightCityBot.cogs.player_inventory.ensure_character_active", AsyncMock(return_value=True))

        ctx = _ctx(author_id=900)
        player = _make_member(999)

        async def run():
            cmd = getattr(cog, "inv_add")
            return await cmd.callback(
                cog, ctx, player, "Kiroshi Optics Mk.1", 2, "V", "cyberware", "basic", "", 3000
            )

        _run(run())

        assert len(added_items) == 2
        assert all(d["item_type"] == "cyberware" for d in added_items)
        assert all(d["restriction"] == "basic" for d in added_items)
        assert all(d["price_paid"] == 3000 for d in added_items)
        assert "✅" in ctx.send.call_args[0][0]

    def test_invalid_restriction_rejected(self, monkeypatch):
        """!inv_add rejects unknown restriction values."""
        cog = _make_cog(monkeypatch)
        monkeypatch.setattr("NightCityBot.cogs.player_inventory.pi_add_item", AsyncMock(return_value=True))
        monkeypatch.setattr("NightCityBot.cogs.player_inventory.get_character_by_name", AsyncMock(return_value=self._CHAR_RECORD))
        monkeypatch.setattr("NightCityBot.cogs.player_inventory.ensure_character_active", AsyncMock(return_value=True))
        ctx = _ctx(author_id=900)
        player = _make_member(999)

        async def run():
            cmd = getattr(cog, "inv_add")
            return await cmd.callback(
                cog, ctx, player, "Some Gun", 1, "V", "gun", "legendary"
            )

        _run(run())
        reply = ctx.send.call_args[0][0]
        assert "❌" in reply
        assert "restriction" in reply.lower()

    def test_restricted_gun_no_note_in_basic(self, monkeypatch):
        """Confirmation message omits restriction note when restriction=basic."""
        cog = _make_cog(monkeypatch)
        monkeypatch.setattr("NightCityBot.cogs.player_inventory.pi_add_item", AsyncMock(return_value=True))
        monkeypatch.setattr("NightCityBot.cogs.player_inventory.get_character_by_name", AsyncMock(return_value=self._CHAR_RECORD))
        monkeypatch.setattr("NightCityBot.cogs.player_inventory.ensure_character_active", AsyncMock(return_value=True))
        ctx = _ctx(author_id=900)
        player = _make_member(999)

        async def run():
            cmd = getattr(cog, "inv_add")
            return await cmd.callback(
                cog, ctx, player, "Basic Pistol", 1, "V", "gun", "basic"
            )

        _run(run())
        reply = ctx.send.call_args[0][0]
        assert "✅" in reply
        assert "[basic]" not in reply  # basic restriction is silent

    def test_keyword_syntax_rejected(self, monkeypatch):
        """Using key=value syntax (e.g. item_type=gun) gives a clear corrective error."""
        cog = _make_cog(monkeypatch)
        monkeypatch.setattr("NightCityBot.cogs.player_inventory.pi_add_item", AsyncMock(return_value=True))
        ctx = _ctx(author_id=900)
        player = _make_member(999)

        async def run():
            cmd = getattr(cog, "inv_add")
            # Simulates: !inv_add @player item 1 char item_type=gun restriction=basic
            return await cmd.callback(
                cog, ctx, player, "item", 1, "char", "item_type=gun", "restriction=basic"
            )

        _run(run())
        reply = ctx.send.call_args[0][0]
        assert "❌" in reply
        assert "positional" in reply.lower()
        assert "Example" in reply


# ------------------------------------------------------------------
# TestInvRemove
# ------------------------------------------------------------------

class TestInvRemove:
    def test_removes_by_uuid(self, monkeypatch):
        cog = _make_cog(monkeypatch)
        item_uuid = str(uuid.uuid4())
        item = {**_item("Kiroshi"), "item_id": item_uuid, "owner_id": "999"}

        monkeypatch.setattr("NightCityBot.cogs.player_inventory.pi_get_item", AsyncMock(return_value=item))
        deleted = []

        async def capture_delete(iid):
            deleted.append(iid)
            return True

        monkeypatch.setattr("NightCityBot.cogs.player_inventory.pi_delete_item", capture_delete)

        ctx = _ctx(author_id=900)
        player = _make_member(999)

        async def run():
            cmd = getattr(cog, "inv_remove")
            return await cmd.callback(cog, ctx, player, item_uuid)

        _run(run())
        assert deleted == [item_uuid]
        assert "✅" in ctx.send.call_args[0][0]

    def test_wrong_owner_rejected(self, monkeypatch):
        cog = _make_cog(monkeypatch)
        item_uuid = str(uuid.uuid4())
        item = {**_item("Kiroshi"), "item_id": item_uuid, "owner_id": "999"}

        monkeypatch.setattr("NightCityBot.cogs.player_inventory.pi_get_item", AsyncMock(return_value=item))
        monkeypatch.setattr("NightCityBot.cogs.player_inventory.pi_delete_item", AsyncMock(return_value=True))

        ctx = _ctx(author_id=900)
        # Different player
        player = _make_member(777)

        async def run():
            cmd = getattr(cog, "inv_remove")
            return await cmd.callback(cog, ctx, player, item_uuid)

        _run(run())
        assert "does not belong" in ctx.send.call_args[0][0]

    def test_not_found_rejects(self, monkeypatch):
        cog = _make_cog(monkeypatch)
        monkeypatch.setattr("NightCityBot.cogs.player_inventory.pi_get_item", AsyncMock(return_value=None))
        ctx = _ctx(author_id=900)

        async def run():
            cmd = getattr(cog, "inv_remove")
            return await cmd.callback(cog, ctx, _make_member(999), "nonexistent-uuid")

        _run(run())
        assert "not found" in ctx.send.call_args[0][0]


# ------------------------------------------------------------------
# TestInvReassign
# ------------------------------------------------------------------

class TestInvReassign:
    def test_reassigns_by_uuid_item_id_first(self, monkeypatch):
        """Signature is (item_id, @player, character_name) — item_id comes first."""
        cog = _make_cog(monkeypatch)
        item_uuid = str(uuid.uuid4())
        item = {**_item("Kiroshi", char="V"), "item_id": item_uuid, "owner_id": "999"}

        monkeypatch.setattr("NightCityBot.cogs.player_inventory.pi_get_item", AsyncMock(return_value=item))
        updated = []

        async def capture_update(iid, new_char):
            updated.append((iid, new_char))
            return True

        monkeypatch.setattr("NightCityBot.cogs.player_inventory.pi_update_character", capture_update)

        ctx = _ctx(author_id=900)
        player = _make_member(999)

        async def run():
            cmd = getattr(cog, "inv_reassign")
            return await cmd.callback(cog, ctx, item_uuid, player, new_character="Johnny")

        _run(run())
        assert updated == [(item_uuid, "Johnny")]
        assert "✅" in ctx.send.call_args[0][0]

    def test_wrong_owner_rejected(self, monkeypatch):
        cog = _make_cog(monkeypatch)
        item_uuid = str(uuid.uuid4())
        item = {**_item("Kiroshi"), "item_id": item_uuid, "owner_id": "999"}

        monkeypatch.setattr("NightCityBot.cogs.player_inventory.pi_get_item", AsyncMock(return_value=item))
        monkeypatch.setattr("NightCityBot.cogs.player_inventory.pi_update_character", AsyncMock(return_value=True))

        ctx = _ctx(author_id=900)

        async def run():
            cmd = getattr(cog, "inv_reassign")
            return await cmd.callback(cog, ctx, item_uuid, _make_member(777), new_character="Johnny")

        _run(run())
        assert "does not belong" in ctx.send.call_args[0][0]


# ------------------------------------------------------------------
# Coverage-gap tests: system-disabled guards, DM guards, page syntax
# ------------------------------------------------------------------

class TestSystemDisabledGuards:
    """Cover the _inv_system_enabled() False path and offline message in each command."""

    def _make_disabled_cog(self, monkeypatch):
        cog = _make_cog(monkeypatch)
        control = MagicMock()
        control.is_enabled = MagicMock(return_value=False)
        cog.bot.get_cog = MagicMock(return_value=control)
        return cog

    def test_inv_system_enabled_returns_false_when_disabled(self, monkeypatch):
        """_inv_system_enabled returns False when SystemControl says disabled."""
        cog = self._make_disabled_cog(monkeypatch)
        assert cog._inv_system_enabled() is False

    def test_my_inventory_offline_message(self, monkeypatch):
        cog = self._make_disabled_cog(monkeypatch)
        monkeypatch.setattr("NightCityBot.cogs.player_inventory.pi_get_by_owner", AsyncMock(return_value=[]))
        ctx = _ctx()
        _run(_cmd(cog, "my_inventory", ctx))
        assert "offline" in ctx.send.call_args[0][0]

    def test_trade_dm_guard(self, monkeypatch):
        """!trade in a DM context is rejected."""
        cog = _make_cog(monkeypatch)
        ctx = _ctx(guild=False)
        _run(_cmd(cog, "trade", ctx, _make_member(999), 1, 100, buyer_character="V"))
        assert "server" in ctx.send.call_args[0][0]

    def test_trade_system_disabled(self, monkeypatch):
        cog = self._make_disabled_cog(monkeypatch)
        monkeypatch.setattr("NightCityBot.cogs.player_inventory.pi_get_by_owner", AsyncMock(return_value=[]))
        ctx = _ctx()
        _run(_cmd(cog, "trade", ctx, _make_member(999), 1, 100, buyer_character="V"))
        assert "offline" in ctx.send.call_args[0][0]

    def test_inv_give_dm_guard(self, monkeypatch):
        """!inv_give in a DM context is rejected."""
        cog = _make_cog(monkeypatch)
        ctx = _ctx(guild=False)
        _run(_cmd(cog, "inv_give", ctx, _make_member(999), 1, "V", "Johnny"))
        assert "server" in ctx.send.call_args[0][0]

    def test_inv_give_system_disabled(self, monkeypatch):
        cog = self._make_disabled_cog(monkeypatch)
        monkeypatch.setattr("NightCityBot.cogs.player_inventory.pi_get_by_owner", AsyncMock(return_value=[]))
        ctx = _ctx()
        _run(_cmd(cog, "inv_give", ctx, _make_member(999), 1, "V", "Johnny"))
        assert "offline" in ctx.send.call_args[0][0]


class TestMyInventoryPageSyntax:
    """Cover page-number parsing branches."""

    def test_page_keyword_syntax(self, monkeypatch):
        """'page N' keyword form is parsed correctly."""
        cog = _make_cog(monkeypatch)
        items = [_item("Kiroshi", char="V")]
        monkeypatch.setattr("NightCityBot.cogs.player_inventory.pi_get_by_owner", AsyncMock(return_value=items))
        ctx = _ctx()
        # "page 1" keyword form — should render page 1 of 1 without error
        _run(_cmd(cog, "my_inventory", ctx, query="page 1"))
        assert ctx.send.called

    def test_trailing_digit_page_syntax(self, monkeypatch):
        """Bare trailing digit after character name is treated as page number."""
        cog = _make_cog(monkeypatch)
        items = [_item("Kiroshi", char="V")]
        monkeypatch.setattr("NightCityBot.cogs.player_inventory.pi_get_by_owner", AsyncMock(return_value=items))
        ctx = _ctx()
        # "V 1" — 'V' is char filter, '1' is page number
        _run(_cmd(cog, "my_inventory", ctx, query="V 1"))
        assert ctx.send.called
