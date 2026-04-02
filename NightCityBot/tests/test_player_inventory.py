"""Tests for the PlayerInventoryCog — my_inventory, trade, inv_give, inv_add/remove/reassign."""

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from NightCityBot.cogs.player_inventory import PlayerInventoryCog


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
    return m


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
            _item("Kiroshi", date="2026-04-02"),
            _item("Kiroshi", date="2026-04-01"),
        ]
        groups = PlayerInventoryCog._group_items(items)
        # FIFO: earliest acquired_at first
        assert groups[0]["items"][0]["acquired_at"] == "2026-04-01"


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
        ctx = _ctx(author_id=111)
        _run(_cmd(cog, "trade", ctx, _make_member(999), 1, 2000, buyer_character="V"))
        msg = ctx.send.call_args[0][0]
        assert "controlled" in msg

    def test_restricted_gun_blocked(self, monkeypatch):
        cog = _make_cog(monkeypatch)
        item = _item("Militech", item_type="gun", restriction="restricted")
        monkeypatch.setattr("NightCityBot.cogs.player_inventory.pi_get_by_owner", AsyncMock(return_value=[item]))
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
        cog = _make_cog(monkeypatch)
        item = _item("Kiroshi")
        monkeypatch.setattr("NightCityBot.cogs.player_inventory.pi_get_by_owner", AsyncMock(return_value=[item]))
        cog.unbelievaboat.get_balance = AsyncMock(return_value={"cash": 100, "bank": 0})
        ctx = _ctx(author_id=111)
        _run(_cmd(cog, "trade", ctx, _make_member(999), 1, 5000, buyer_character="V"))
        assert "cannot afford" in ctx.send.call_args[0][0]

    def test_db_failure_creates_pending_transfer(self, monkeypatch):
        """If seller credit fails after buyer debit, a pending_transfers record is created."""
        cog = _make_cog(monkeypatch)
        item = _item("Kiroshi", item_type="cyberware")
        monkeypatch.setattr("NightCityBot.cogs.player_inventory.pi_get_by_owner", AsyncMock(return_value=[item]))

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
        assert "⚠️" in ctx.send.call_args[0][0]

    def test_success_logs_to_correct_channel(self, monkeypatch):
        """Gun item trade logs to gun-log channel."""
        cog = _make_cog(monkeypatch)
        item = _item("Liberty", item_type="gun")
        monkeypatch.setattr("NightCityBot.cogs.player_inventory.pi_get_by_owner", AsyncMock(return_value=[item]))
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
    def test_qty_creates_multiple_rows(self, monkeypatch):
        cog = _make_cog(monkeypatch)
        added_items = []

        async def capture_add(item_dict):
            added_items.append(item_dict)
            return True

        monkeypatch.setattr("NightCityBot.cogs.player_inventory.pi_add_item", capture_add)

        ctx = _ctx(author_id=900)
        ctx.author.roles = [MagicMock(id=900)]  # fixer role

        player = _make_member(999)

        async def run():
            cmd = getattr(cog, "inv_add")
            return await cmd.callback(cog, ctx, player, "Trauma Team Card", 3, "V", "gear", "", None)

        _run(run())

        # 3 separate rows with unique UUIDs
        assert len(added_items) == 3
        item_ids = [d["item_id"] for d in added_items]
        assert len(set(item_ids)) == 3  # all unique UUIDs
        assert all(d["name"] == "Trauma Team Card" for d in added_items)
        assert all(d["character_name"] == "V" for d in added_items)
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
