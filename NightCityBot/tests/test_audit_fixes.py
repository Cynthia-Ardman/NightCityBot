import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import discord
from discord.ext import commands
import config


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_cyberware_cog():
    bot = MagicMock()
    bot.loop = asyncio.new_event_loop()
    bot.unbelievaboat = MagicMock()
    bot.get_cog = MagicMock(return_value=None)
    bot.get_guild = MagicMock(return_value=None)
    with patch("asyncio.create_task", lambda *a, **k: None):
        from NightCityBot.cogs.cyberware import CyberwareManager
        cog = CyberwareManager(bot)
    return cog


class TestNegativeCashSplit:
    def test_negative_cash_clamps_to_zero(self):
        cog = _make_cyberware_cog()

        guild = MagicMock()
        approved_role = MagicMock()
        approved_role.id = config.APPROVED_ROLE_ID
        checkup_role = MagicMock()
        medium_role = MagicMock()
        high_role = MagicMock()
        extreme_role = MagicMock()
        loa_role = MagicMock()
        ripper_role = MagicMock()
        log_channel = MagicMock()
        log_channel.send = AsyncMock()

        member = MagicMock()
        member.id = 12345
        member.roles = [approved_role, checkup_role, medium_role]
        member.add_roles = AsyncMock()
        member.remove_roles = AsyncMock()

        guild.get_role = MagicMock(side_effect=lambda rid: {
            config.CYBER_CHECKUP_ROLE_ID: checkup_role,
            config.CYBER_MEDIUM_ROLE_ID: medium_role,
            config.CYBER_HIGH_ROLE_ID: high_role,
            config.CYBER_EXTREME_ROLE_ID: extreme_role,
            config.LOA_ROLE_ID: loa_role,
            config.RIPPERDOC_ROLE_ID: ripper_role,
        }.get(rid))
        guild.get_channel = MagicMock(return_value=log_channel)
        guild.members = [member]

        cog.bot.get_guild = MagicMock(return_value=guild)
        cog.bot.get_cog = MagicMock(return_value=None)

        cog.unbelievaboat.get_balance = AsyncMock(return_value={"cash": -200, "bank": 5000})
        cog.unbelievaboat.update_balance = AsyncMock(return_value=True)

        cog.data = {"12345": {"weeks": 1, "last": None}}

        with (
            patch("NightCityBot.cogs.cyberware.cyberware_last_run_set", new=AsyncMock(return_value=True)),
            patch("NightCityBot.cogs.cyberware.cyberware_status_upsert_many", new=AsyncMock(return_value=True)),
        ):
            logs = []
            _run(cog.process_week(log=logs, target_member=member))

        assert cog.unbelievaboat.update_balance.called
        call_args = cog.unbelievaboat.update_balance.call_args
        payload = call_args[0][1]
        assert payload["cash"] == 0
        assert payload["bank"] < 0


class TestCollectCyberwareNoLastEntry:
    def test_no_prior_weekly_data_does_not_crash(self):
        cog = _make_cyberware_cog()

        guild = MagicMock()
        member = MagicMock()
        member.id = 999
        member.display_name = "TestUser"
        member.roles = [MagicMock(id=config.APPROVED_ROLE_ID)]

        cog.bot.get_guild = MagicMock(return_value=guild)
        cog.bot.get_cog = MagicMock(return_value=None)

        ctx = MagicMock()
        ctx.guild = guild
        ctx.author = MagicMock()
        ctx.send = AsyncMock()

        with (
            patch("NightCityBot.cogs.cyberware.cyberware_weekly_get_last_row", new=AsyncMock(return_value=(None, None))),
            patch("NightCityBot.cogs.cyberware.cyberware_weekly_insert_empty", new=AsyncMock(return_value=1)),
            patch("NightCityBot.cogs.cyberware.cyberware_weekly_update_row", new=AsyncMock(return_value=True)),
            patch("NightCityBot.cogs.cyberware.cyberware_last_run_set", new=AsyncMock(return_value=True)),
            patch("NightCityBot.cogs.cyberware.cyberware_status_upsert_many", new=AsyncMock(return_value=True)),
        ):
            cmd = getattr(cog, "collect_cyberware")
            _run(cmd.callback(cog, ctx, member))

        assert ctx.send.called


class TestPayCyberwareNoLastEntry:
    def test_no_prior_weekly_data_does_not_crash(self):
        cog = _make_cyberware_cog()

        guild = MagicMock()
        author = MagicMock(spec=discord.Member)
        author.id = 888
        author.display_name = "Player"
        author.roles = [MagicMock(id=config.APPROVED_ROLE_ID)]

        ctx = MagicMock()
        ctx.guild = guild
        ctx.author = author

        ctx.send = AsyncMock()

        cog.bot.get_guild = MagicMock(return_value=guild)
        cog.bot.get_cog = MagicMock(return_value=None)

        with (
            patch("NightCityBot.cogs.cyberware.cyberware_weekly_get_last_row", new=AsyncMock(return_value=(None, None))),
            patch("NightCityBot.cogs.cyberware.cyberware_weekly_insert_empty", new=AsyncMock(return_value=1)),
            patch("NightCityBot.cogs.cyberware.cyberware_weekly_update_row", new=AsyncMock(return_value=True)),
            patch("NightCityBot.cogs.cyberware.cyberware_last_run_set", new=AsyncMock(return_value=True)),
            patch("NightCityBot.cogs.cyberware.cyberware_status_upsert_many", new=AsyncMock(return_value=True)),
        ):
            cmd = getattr(cog, "pay_cyberware")
            _run(cmd.callback(cog, ctx))

        assert ctx.send.called


class TestTraumaBackupLabels:
    def test_labels_are_trauma_not_cyberware(self):
        from NightCityBot.services.trauma_team import TraumaTeamService

        bot = MagicMock()
        service = TraumaTeamService(bot)

        member = MagicMock()
        member.id = 777
        member.roles = [MagicMock(id=config.LOA_ROLE_ID + 999, name="Platinum")]

        bot.get_channel.return_value = MagicMock(spec=discord.ForumChannel)

        economy = MagicMock()
        economy.unbelievaboat = MagicMock()
        economy.unbelievaboat.get_balance = AsyncMock(return_value={"cash": 10000, "bank": 0})
        economy.unbelievaboat.update_balance = AsyncMock(return_value=True)
        economy.backup_balances = AsyncMock()
        bot.get_cog = MagicMock(side_effect=lambda n: {
            "SystemControl": None,
            "Economy": economy,
        }.get(n))

        trauma_thread = MagicMock()
        trauma_thread.send = AsyncMock()

        with patch.object(service, "process_trauma_team_payment", wraps=service.process_trauma_team_payment):
            pass

        from NightCityBot.utils import config_loader as _cfg
        with patch.object(_cfg, "get_trauma_role_costs", return_value={"Platinum": 2000}):
            forum = MagicMock(spec=discord.ForumChannel)
            forum.threads = []

            async def mock_archived(limit=None):
                return
                yield

            forum.archived_threads = mock_archived
            bot.get_channel = MagicMock(return_value=forum)

            _run(service.process_trauma_team_payment(member, log=[]))

        labels = [call.kwargs.get("label", call[0][1] if len(call[0]) > 1 else None)
                  for call in economy.backup_balances.call_args_list]
        for label in labels:
            assert "trauma" in label
            assert "cyberware" not in label


class TestDmDeadCodeRemoved:
    def test_dm_command_no_dead_code_branches(self):
        from NightCityBot.cogs.dm_handling import DMHandler
        import inspect

        source = inspect.getsource(DMHandler.dm.callback)
        assert "raise ValueError" not in source
        assert "User fetch returned None" not in source
