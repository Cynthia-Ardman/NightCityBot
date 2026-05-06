"""Tests for the admin Balance History panel helpers."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from NightCityBot.cogs.admin_shop import (
    _friendly_backup_label,
    _load_backup_history,
    _merge_history,
    _format_history_lines,
    _fit_lines_to_description,
    _parse_ub_amount,
    _parse_ub_balance_embed,
    _load_economy_log_history,
    _candidate_names_for,
    _ub_line_matches,
)
from NightCityBot.services.unbelievaboat import UnbelievaBoatAPI
import config


class TestFriendlyBackupLabel:
    def test_drops_before_anchor_entries(self):
        assert _friendly_backup_label("collect_housing_before") is None
        assert _friendly_backup_label("trauma_before") is None

    def test_maps_known_after_labels(self):
        assert _friendly_backup_label("collect_housing_after") == "Housing Rent collected"
        assert _friendly_backup_label("collect_business_after") == "Business Rent collected"
        assert _friendly_backup_label("collect_cyberware_after") == "Cyberware Rent collected"
        assert _friendly_backup_label("trauma_after") == "Trauma Team service"

    def test_manual_prefix(self):
        assert _friendly_backup_label("manual_20260101_120000.json") == "Admin manual backup/restore"

    def test_unknown_label_passthrough(self):
        assert _friendly_backup_label("custom_thing") == "custom_thing"

    def test_empty(self):
        assert _friendly_backup_label("") is None


class TestMergeHistory:
    def _row(self, ts, cash=0, bank=0, reason="r"):
        return {"id": None, "ts": ts, "cash_delta": cash, "bank_delta": bank, "reason": reason}

    def test_merges_and_sorts_desc(self):
        now = datetime.now(timezone.utc)
        live = [self._row(now, cash=-500, reason="Housing Rent")]
        backup = [self._row(now - timedelta(hours=1), cash=-1000, reason="Cyberware Rent (snapshot)")]
        merged = _merge_history(live, backup)
        assert len(merged) == 2
        assert merged[0]["ts"] >= merged[1]["ts"]

    def test_drops_backup_dupe_within_window(self):
        now = datetime.now(timezone.utc)
        live = [self._row(now, cash=-500, reason="Housing Rent")]
        backup = [self._row(now + timedelta(seconds=30), cash=-500, reason="Housing Rent collected (snapshot)")]
        merged = _merge_history(live, backup)
        assert len(merged) == 1
        assert merged[0]["reason"] == "Housing Rent"

    def test_keeps_backup_when_amounts_differ(self):
        now = datetime.now(timezone.utc)
        live = [self._row(now, cash=-500, reason="Housing Rent")]
        backup = [self._row(now + timedelta(seconds=30), cash=-200, reason="Other event (snapshot)")]
        merged = _merge_history(live, backup)
        assert len(merged) == 2

    def test_does_not_collapse_same_net_but_different_split(self):
        """A withdraw (cash=+500, bank=-500, net 0) and a no-op snapshot
        (cash=0, bank=0, net 0) share the same net total but represent
        different events. Both must survive the merge."""
        now = datetime.now(timezone.utc)
        live = [self._row(now, cash=500, bank=-500, reason="ATM withdraw")]
        backup = [self._row(now + timedelta(seconds=10), cash=0, bank=0, reason="snapshot")]
        merged = _merge_history(live, backup)
        assert len(merged) == 2

    def test_keeps_backup_when_outside_window(self):
        now = datetime.now(timezone.utc)
        live = [self._row(now, cash=-500, reason="Housing Rent")]
        backup = [self._row(now + timedelta(minutes=10), cash=-500, reason="Different rent (snapshot)")]
        merged = _merge_history(live, backup)
        assert len(merged) == 2


class TestFormatHistoryLines:
    def test_renders_cash_and_bank_deltas(self):
        rows = [{
            "ts": datetime(2026, 4, 29, 12, 30, tzinfo=timezone.utc),
            "cash_delta": -500,
            "bank_delta": 200,
            "reason": "Some thing",
        }]
        lines, omitted = _format_history_lines(rows)
        assert omitted == 0
        assert len(lines) == 1
        assert "$-500 cash" in lines[0]
        assert "+$200 bank" in lines[0]
        assert "Some thing" in lines[0]

    def test_truncates_long_reasons(self):
        rows = [{
            "ts": datetime(2026, 4, 29, 12, 30, tzinfo=timezone.utc),
            "cash_delta": -1,
            "bank_delta": 0,
            "reason": "x" * 500,
        }]
        lines, _ = _format_history_lines(rows)
        assert lines[0].count("x") <= 140

    def test_caps_max_rows(self):
        ts = datetime(2026, 4, 29, 12, 0, tzinfo=timezone.utc)
        rows = [{"ts": ts, "cash_delta": -1, "bank_delta": 0, "reason": str(i)} for i in range(75)]
        lines, omitted = _format_history_lines(rows, max_rows=50)
        assert len(lines) == 50
        assert omitted == 25


class TestFitLines:
    def test_fits_within_cap(self):
        lines = ["short line"] * 5
        desc, dropped = _fit_lines_to_description(lines, cap=200)
        assert dropped == 0
        assert desc.count("\n") == 4

    def test_drops_overflow(self):
        lines = ["x" * 100 for _ in range(50)]
        desc, dropped = _fit_lines_to_description(lines, cap=300)
        assert dropped > 0
        assert len(desc) <= 300


def test_update_balance_records_history_on_success():
    api = UnbelievaBoatAPI.__new__(UnbelievaBoatAPI)
    with patch(
        "NightCityBot.utils.db.balance_history_record",
        new=AsyncMock(return_value=True),
    ) as mock_record:
        asyncio.run(api._record_history(
            12345, {"cash": -500, "bank": 0}, "Housing Rent"
        ))
        mock_record.assert_awaited_once_with("12345", -500, 0, "Housing Rent")


def test_record_history_swallows_db_failure():
    api = UnbelievaBoatAPI.__new__(UnbelievaBoatAPI)
    with patch(
        "NightCityBot.utils.db.balance_history_record",
        new=AsyncMock(side_effect=RuntimeError("db down")),
    ):
        # Must not raise
        asyncio.run(api._record_history(1, {"cash": 0}, "x"))


def test_update_balance_invokes_record_history_after_200_patch():
    """End-to-end: a successful PATCH must trigger _record_history."""
    api = UnbelievaBoatAPI.__new__(UnbelievaBoatAPI)
    api.api_token = "t"
    api.base_url = "https://example.invalid/g"
    api.headers = {"Authorization": "t"}
    api._semaphore = asyncio.Semaphore(1)
    api._record_history = AsyncMock()

    class _Resp:
        status = 200
        async def text(self):
            return ""
        async def json(self):
            return {}
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False

    api.session = MagicMock()
    api.session.patch = MagicMock(return_value=_Resp())

    ok = asyncio.run(api.update_balance(42, {"cash": -250, "bank": 0}, reason="Housing Rent"))
    assert ok is True
    api._record_history.assert_awaited_once_with(42, {"cash": -250, "bank": 0}, "Housing Rent")


def test_update_balance_does_not_record_when_patch_fails():
    api = UnbelievaBoatAPI.__new__(UnbelievaBoatAPI)
    api.api_token = "t"
    api.base_url = "https://example.invalid/g"
    api.headers = {"Authorization": "t"}
    api._semaphore = asyncio.Semaphore(1)
    api._record_history = AsyncMock()

    class _Resp:
        status = 500
        async def text(self):
            return "boom"
        async def json(self):
            return {}
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False

    api.session = MagicMock()
    api.session.patch = MagicMock(return_value=_Resp())

    with patch("asyncio.sleep", new=AsyncMock()):
        ok = asyncio.run(api.update_balance(7, {"cash": 1}, reason="x"))
    assert ok is False
    api._record_history.assert_not_called()


class TestParseUBAmount:
    def test_simple_positive(self):
        assert _parse_ub_amount("+477") == 477

    def test_negative(self):
        assert _parse_ub_amount("-200") == -200

    def test_thousands_separator(self):
        assert _parse_ub_amount("-3,000") == -3000
        assert _parse_ub_amount("+6,000") == 6000

    def test_zero_and_garbage(self):
        assert _parse_ub_amount("0") == 0
        assert _parse_ub_amount("nope") == 0


def _make_embed(*, description: str = "", title: str = "", fields=None) -> discord.Embed:
    kwargs = {}
    if title:
        kwargs["title"] = title
    if description:
        kwargs["description"] = description
    e = discord.Embed(**kwargs)
    for fname, fvalue in (fields or []):
        e.add_field(name=fname, value=fvalue, inline=False)
    return e


class TestParseUBBalanceEmbed:
    TARGET = 286338318076084226
    OTHER = 999000111222333

    def test_parses_self_action_credit(self):
        """work/crime/etc — User: <@target>, no actor."""
        e = _make_embed(description=(
            "Balance updated\n"
            f"User: <@{self.TARGET}>\n"
            "Amount: Cash: +477 | Bank: 0\n"
            "Reason: crime command"
        ))
        row = _parse_ub_balance_embed(e, self.TARGET)
        assert row is not None
        assert row["cash_delta"] == 477
        assert row["bank_delta"] == 0
        assert "crime command" in row["reason"]
        assert row["reason"].startswith("UB:")

    def test_parses_negative_with_thousands(self):
        e = _make_embed(description=(
            f"Balance updated\nUser: <@{self.TARGET}>\n"
            "Amount: Cash: -3,000 | Bank: 0\nReason: roulette bet"
        ))
        row = _parse_ub_balance_embed(e, self.TARGET)
        assert row is not None
        assert row["cash_delta"] == -3000

    def test_give_money_outgoing_side(self):
        """Sender side of give-money: actor == target."""
        e = _make_embed(description=(
            f"Balance updated\nUser: <@{self.TARGET}>\n"
            f"Actioned by: <@{self.TARGET}>\n"
            "Amount: Cash: -200 | Bank: 0\nReason: give-money command"
        ))
        row = _parse_ub_balance_embed(e, self.TARGET)
        assert row is not None
        assert row["cash_delta"] == -200
        # Self-action — should NOT tack on a "by <@x>" suffix
        assert " — by <@" not in row["reason"]

    def test_give_money_incoming_side_tags_actor(self):
        """Receiver side: User=target, actor=different. Reason gets actor tag."""
        e = _make_embed(description=(
            f"Balance updated\nUser: <@{self.TARGET}>\n"
            f"Actioned by: <@{self.OTHER}>\n"
            "Amount: Cash: +700 | Bank: 0\nReason: give-money command"
        ))
        row = _parse_ub_balance_embed(e, self.TARGET)
        assert row is not None
        assert row["cash_delta"] == 700
        assert f"<@{self.OTHER}>" in row["reason"]

    def test_skips_embed_for_other_user(self):
        e = _make_embed(description=(
            f"Balance updated\nUser: <@{self.OTHER}>\n"
            "Amount: Cash: +1000 | Bank: 0\nReason: work command"
        ))
        assert _parse_ub_balance_embed(e, self.TARGET) is None

    def test_skips_zero_delta_rows(self):
        e = _make_embed(description=(
            f"Balance updated\nUser: <@{self.TARGET}>\n"
            "Amount: Cash: 0 | Bank: 0\nReason: noop"
        ))
        assert _parse_ub_balance_embed(e, self.TARGET) is None

    def test_handles_bank_movement(self):
        e = _make_embed(description=(
            f"Balance updated\nUser: <@{self.TARGET}>\n"
            "Amount: Cash: 0 | Bank: +500\nReason: deposit command"
        ))
        row = _parse_ub_balance_embed(e, self.TARGET)
        assert row is not None
        assert row["cash_delta"] == 0
        assert row["bank_delta"] == 500

    def test_field_based_embed_format(self):
        """Same data but in fields instead of description (alt UB format)."""
        e = _make_embed(
            title="Balance updated",
            fields=[
                ("User", f"<@{self.TARGET}>"),
                ("Amount", "Cash: +193 | Bank: 0"),
                ("Reason", "work command"),
            ],
        )
        row = _parse_ub_balance_embed(e, self.TARGET)
        assert row is not None
        assert row["cash_delta"] == 193
        assert "work command" in row["reason"]

    def test_empty_embed_returns_none(self):
        assert _parse_ub_balance_embed(_make_embed(), self.TARGET) is None

    def test_blackjack_pair(self):
        """Two embeds (bet then ended) both target user — both parse independently."""
        bet = _make_embed(description=(
            f"Balance updated\nUser: <@{self.TARGET}>\n"
            "Amount: Cash: -302 | Bank: 0\nReason: blackjack bet"
        ))
        won = _make_embed(description=(
            f"Balance updated\nUser: <@{self.TARGET}>\n"
            "Amount: Cash: +604 | Bank: 0\nReason: blackjack ended"
        ))
        r1 = _parse_ub_balance_embed(bet, self.TARGET)
        r2 = _parse_ub_balance_embed(won, self.TARGET)
        assert r1 and r2
        assert r1["cash_delta"] == -302 and r2["cash_delta"] == 604


class TestCandidateNamesFor:
    def test_collects_all_distinct_names(self):
        m = MagicMock()
        m.name = "medusa"
        m.global_name = "Medusa"
        m.display_name = "Shadow the Edgehog"
        m.nick = "Shadow the Edgehog"
        names = _candidate_names_for(m)
        assert "medusa" in names
        assert "shadow the edgehog" in names
        # combo form for UB plaintext mentions
        assert "medusa (shadow the edgehog)" in names

    def test_handles_none(self):
        assert _candidate_names_for(None) == []

    def test_no_combo_when_name_equals_display(self):
        m = MagicMock()
        m.name = "alice"
        m.global_name = None
        m.display_name = "alice"
        m.nick = None
        names = _candidate_names_for(m)
        assert names == ["alice"]


class TestUBLineMatches:
    def test_snowflake_match(self):
        assert _ub_line_matches("<@123>", 123, []) is True
        assert _ub_line_matches("<@!456>", 456, []) is True
        assert _ub_line_matches("<@123>", 999, ["medusa"]) is False  # snowflake wins

    def test_name_fallback(self):
        assert _ub_line_matches(
            "@Medusa (Shadow the Edgehog)", 12345, ["medusa (shadow the edgehog)"]
        ) is True

    def test_name_substring(self):
        assert _ub_line_matches("@medusa", 1, ["medusa"]) is True

    def test_no_match_when_neither_works(self):
        assert _ub_line_matches("@bob", 1, ["alice"]) is False

    def test_empty_line(self):
        assert _ub_line_matches("", 1, ["alice"]) is False


class TestParseUBBalanceEmbedNameFallback:
    """Real-world: UB writes /give-money mentions as plain '@Username (Display)'."""
    TARGET = 286338318076084226

    def test_plaintext_mention_matches_via_names(self):
        e = _make_embed(description=(
            "Balance updated\n"
            "User: @Medusa (Shadow the Edgehog)\n"
            "Actioned by: @Medusa (Shadow the Edgehog)\n"
            "Amount: Cash: -1 | Bank: 0\n"
            "Reason: give-money command"
        ))
        names = ["medusa", "shadow the edgehog", "medusa (shadow the edgehog)"]
        row = _parse_ub_balance_embed(e, self.TARGET, names)
        assert row is not None
        assert row["cash_delta"] == -1
        assert "give-money" in row["reason"]
        # Self-action — actor matched same names, should NOT add "by …"
        assert " — by " not in row["reason"]

    def test_plaintext_other_user_does_not_match(self):
        e = _make_embed(description=(
            "Balance updated\n"
            "User: @Vinny Russo/Vanessa Bitch\n"
            "Actioned by: @Medusa (Shadow the Edgehog)\n"
            "Amount: Cash: +1 | Bank: 0\n"
            "Reason: give-money command"
        ))
        names = ["medusa", "shadow the edgehog", "medusa (shadow the edgehog)"]
        row = _parse_ub_balance_embed(e, self.TARGET, names)
        assert row is None

    def test_plaintext_received_money_tags_actor_text(self):
        """Receiver side via plaintext: actor name kept verbatim in reason."""
        e = _make_embed(description=(
            "Balance updated\n"
            "User: @Vinny Russo/Vanessa Bitch\n"
            "Actioned by: @Medusa (Shadow the Edgehog)\n"
            "Amount: Cash: +1 | Bank: 0\n"
            "Reason: give-money command"
        ))
        # Pretend Vinny is the target now
        names = ["vinny russo/vanessa bitch", "vinny"]
        row = _parse_ub_balance_embed(e, 999, names)
        assert row is not None
        assert row["cash_delta"] == 1
        assert "Medusa" in row["reason"]
        assert " — by " in row["reason"]

    def test_real_world_markdown_formatted_embed(self):
        """UB embeds in production wrap field labels in **bold** and
        numeric values in `code` formatting. Parser must handle both."""
        e = _make_embed(description=(
            "**User:** @Medusa (Shadow the Edgehog)\n"
            "**Actioned by:** @Medusa (Shadow the Edgehog)\n"
            "**Amount:** Cash: `-1` | Bank: `0`\n"
            "**Reason:** give-money command"
        ))
        names = ["medusa", "shadow the edgehog", "medusa (shadow the edgehog)"]
        row = _parse_ub_balance_embed(e, self.TARGET, names)
        assert row is not None
        assert row["cash_delta"] == -1
        assert row["bank_delta"] == 0
        assert "give-money" in row["reason"]

    def test_real_world_markdown_with_thousands(self):
        e = _make_embed(description=(
            "**User:** @Medusa (Shadow the Edgehog)\n"
            "**Amount:** Cash: `-3,000` | Bank: `+500`\n"
            "**Reason:** roulette bet"
        ))
        names = ["medusa", "medusa (shadow the edgehog)"]
        row = _parse_ub_balance_embed(e, self.TARGET, names)
        assert row is not None
        assert row["cash_delta"] == -3000
        assert row["bank_delta"] == 500

    def test_no_target_names_still_works_with_snowflake(self):
        """Existing snowflake path stays functional with empty names list."""
        e = _make_embed(description=(
            f"Balance updated\nUser: <@{self.TARGET}>\n"
            "Amount: Cash: +1 | Bank: 0\nReason: work command"
        ))
        row = _parse_ub_balance_embed(e, self.TARGET, [])
        assert row is not None and row["cash_delta"] == 1


class TestLoadEconomyLogHistory:
    TARGET = 286338318076084226
    UB_BOT_ID = 292953664492929025

    def _make_msg(self, *, author_id: int, embeds: list, ts: datetime):
        msg = MagicMock()
        msg.author = MagicMock()
        msg.author.id = author_id
        msg.embeds = embeds
        msg.created_at = ts
        return msg

    def _make_channel(self, msgs):
        async def _hist(after=None, limit=None, oldest_first=False):
            for m in msgs:
                if after and m.created_at <= after:
                    continue
                yield m
        ch = MagicMock()
        ch.history = MagicMock(side_effect=lambda **kw: _hist(**kw))
        return ch

    def test_parses_messages_regardless_of_author(self):
        """UnbelievaBoat is sometimes delivered via webhook (whose author
        id != the bot id) or via a forked instance. We rely on the
        parser's strict signature match instead of the author id."""
        now = datetime.now(timezone.utc)
        balance_embed = _make_embed(description=(
            f"Balance updated\nUser: <@{self.TARGET}>\n"
            "Amount: Cash: +500 | Bank: 0\nReason: work command"
        ))
        unrelated_embed = _make_embed(description=(
            "Some other bot's embed with no balance signature"
        ))
        msgs = [
            # Webhook-delivered (different author id) — must STILL parse
            self._make_msg(author_id=999999, embeds=[balance_embed], ts=now),
            # Unrelated embed — must NOT parse
            self._make_msg(author_id=11111111, embeds=[unrelated_embed], ts=now),
        ]
        bot = MagicMock()
        bot.get_channel.return_value = self._make_channel(msgs)
        rows = asyncio.run(_load_economy_log_history(bot, self.TARGET, now - timedelta(days=30)))
        assert len(rows) == 1
        assert rows[0]["cash_delta"] == 500
        assert "work" in rows[0]["reason"]

    def test_returns_empty_when_channel_id_unset(self, monkeypatch):
        monkeypatch.setattr(config, "ECONOMY_LOG_CHANNEL_ID", 0)
        bot = MagicMock()
        rows = asyncio.run(_load_economy_log_history(
            bot, self.TARGET, datetime.now(timezone.utc) - timedelta(days=1)
        ))
        assert rows == []
        bot.get_channel.assert_not_called()

    def test_returns_empty_on_forbidden_channel(self):
        bot = MagicMock()
        bot.get_channel.return_value = None
        bot.fetch_channel = AsyncMock(side_effect=discord.Forbidden(MagicMock(status=403, reason="x"), "no"))
        rows = asyncio.run(_load_economy_log_history(
            bot, self.TARGET, datetime.now(timezone.utc) - timedelta(days=1)
        ))
        assert rows == []

    def test_attaches_message_timestamp(self):
        ts = datetime(2026, 5, 1, 12, 30, tzinfo=timezone.utc)
        target_embed = _make_embed(description=(
            f"Balance updated\nUser: <@{self.TARGET}>\n"
            "Amount: Cash: +1 | Bank: 0\nReason: work command"
        ))
        msg = self._make_msg(author_id=self.UB_BOT_ID, embeds=[target_embed], ts=ts)
        bot = MagicMock()
        bot.get_channel.return_value = self._make_channel([msg])
        rows = asyncio.run(_load_economy_log_history(bot, self.TARGET, ts - timedelta(days=1)))
        assert len(rows) == 1
        assert rows[0]["ts"] == ts


def test_economy_log_dedupes_against_internal_live_row():
    """An UB row and a live audit row for the same delta within 120s
    should collapse to a single entry — preventing rent/cyberware
    double-counting since our bot's update_balance also surfaces in UB."""
    now = datetime.now(timezone.utc)
    live = [{"id": 1, "ts": now, "cash_delta": -500, "bank_delta": 0,
             "reason": "Cyberware meds week 1"}]
    ub = [{"id": None, "ts": now + timedelta(seconds=5), "cash_delta": -500,
           "bank_delta": 0, "reason": "UB: Cyberware meds week 1"}]
    merged = _merge_history(live, ub)
    assert len(merged) == 1
    # live row wins because it carries our own (richer) reason text
    assert merged[0]["reason"] == "Cyberware meds week 1"


def test_load_backup_history_skips_malformed_change(tmp_path, monkeypatch):
    """A single bad ``change`` value must not nuke the entire backup file."""
    import config
    monkeypatch.setattr(config, "BALANCE_BACKUP_DIR", str(tmp_path))
    user_id = 999
    now = datetime.now(timezone.utc)
    entries = [
        {  # bad: change is non-numeric
            "timestamp": now.isoformat(),
            "label": "collect_housing_after",
            "change": "not-a-number",
        },
        {  # good
            "timestamp": now.isoformat(),
            "label": "trauma_after",
            "change": -1500,
        },
    ]
    (tmp_path / f"balance_backup_{user_id}.json").write_text(json.dumps(entries))
    rows = asyncio.run(_load_backup_history(user_id, now - timedelta(days=30)))
    assert len(rows) == 1
    assert rows[0]["cash_delta"] == -1500
    assert "Trauma Team service" in rows[0]["reason"]
