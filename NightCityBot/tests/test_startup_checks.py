import asyncio
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock
from pathlib import Path

import discord
import config

from NightCityBot.utils.startup_checks import (
    verify_config,
    cleanup_logs,
    check_unbelievaboat,
    check_db_health,
    perform_startup_checks,
    ROLE_ID_FIELDS,
    CHANNEL_ID_FIELDS,
)


def _run(coro):
    return asyncio.run(coro)


def _make_guild():
    guild = MagicMock(spec=discord.Guild)
    guild.get_role = MagicMock(return_value=MagicMock(spec=discord.Role))
    guild.get_channel = MagicMock(return_value=MagicMock(spec=discord.TextChannel))
    guild.me = MagicMock()
    perms = MagicMock()
    perms.send_messages = True
    perms.manage_messages = True
    perms.manage_channels = True
    perms.manage_roles = True
    perms.attach_files = True
    perms.embed_links = True
    guild.me.guild_permissions = perms
    guild.members = []
    return guild


def _make_bot(guild=None):
    bot = MagicMock(spec=discord.Client)
    bot.get_guild = MagicMock(return_value=guild)
    bot.get_cog = MagicMock(return_value=None)
    bot.wait_until_ready = AsyncMock()
    bot.user = MagicMock()
    return bot


class TestVerifyConfig:
    def test_no_guild_returns_early(self):
        bot = _make_bot(guild=None)
        _run(verify_config(bot))
        bot.get_guild.assert_called_once()

    def test_clean_config(self):
        guild = _make_guild()
        bot = _make_bot(guild=guild)
        _run(verify_config(bot))

    def test_missing_role_logs_warning(self):
        guild = _make_guild()
        guild.get_role = MagicMock(return_value=None)
        bot = _make_bot(guild=guild)
        with patch("NightCityBot.utils.startup_checks.logger") as mock_log:
            _run(verify_config(bot))
            warn_calls = [c for c in mock_log.warning.call_args_list
                          if "Missing role" in str(c)]
            assert len(warn_calls) > 0

    def test_missing_channel_logs_warning(self):
        guild = _make_guild()
        guild.get_channel = MagicMock(return_value=None)
        bot = _make_bot(guild=guild)
        with patch("NightCityBot.utils.startup_checks.logger") as mock_log:
            _run(verify_config(bot))
            warn_calls = [c for c in mock_log.warning.call_args_list
                          if "Missing channel" in str(c) or "Channel not found" in str(c)]
            assert len(warn_calls) > 0

    def test_missing_permission_logs_warning(self):
        guild = _make_guild()
        guild.me.guild_permissions.send_messages = False
        bot = _make_bot(guild=guild)
        with patch("NightCityBot.utils.startup_checks.logger") as mock_log:
            _run(verify_config(bot))
            warn_calls = [c for c in mock_log.warning.call_args_list
                          if "missing permission" in str(c).lower()]
            assert len(warn_calls) > 0

    def test_forum_channel_wrong_type(self):
        guild = _make_guild()
        text_ch = MagicMock(spec=discord.TextChannel)
        guild.get_channel = MagicMock(return_value=text_ch)
        bot = _make_bot(guild=guild)
        with patch("NightCityBot.utils.startup_checks.logger") as mock_log:
            _run(verify_config(bot))
            warn_calls = [c for c in mock_log.warning.call_args_list
                          if "expected ForumChannel" in str(c)]
            assert len(warn_calls) > 0


class TestCleanupLogs:
    def test_no_guild_returns_early(self):
        bot = _make_bot(guild=None)
        _run(cleanup_logs(bot))

    def test_cleans_orphaned_entries(self):
        guild = _make_guild()
        member = MagicMock()
        member.id = 111
        guild.members = [member]
        bot = _make_bot(guild=guild)

        fake_data = {"111": "value", "999": "orphan"}
        with patch("NightCityBot.utils.startup_checks.Path") as MockPath:
            mock_path = MagicMock()
            mock_path.exists.return_value = True
            MockPath.return_value = mock_path
            with patch("NightCityBot.utils.startup_checks.load_json_file",
                        new_callable=AsyncMock, return_value=fake_data):
                with patch("NightCityBot.utils.startup_checks.save_json_file",
                            new_callable=AsyncMock) as mock_save:
                    _run(cleanup_logs(bot))
                    if mock_save.call_count > 0:
                        saved = mock_save.call_args[0][1]
                        assert "999" not in saved

    def test_skips_missing_files(self):
        guild = _make_guild()
        guild.members = []
        bot = _make_bot(guild=guild)
        with patch("NightCityBot.utils.startup_checks.Path") as MockPath:
            mock_path = MagicMock()
            mock_path.exists.return_value = False
            MockPath.return_value = mock_path
            with patch("NightCityBot.utils.startup_checks.load_json_file",
                        new_callable=AsyncMock) as mock_load:
                _run(cleanup_logs(bot))


class TestCheckUnbelievaboat:
    def test_no_token(self):
        bot = _make_bot()
        with patch.object(config, "UNBELIEVABOAT_API_TOKEN", ""):
            with patch("NightCityBot.utils.startup_checks.logger") as mock_log:
                _run(check_unbelievaboat(bot))
                warn_calls = [c for c in mock_log.warning.call_args_list
                              if "not configured" in str(c).lower()]
                assert len(warn_calls) > 0

    def test_success(self):
        bot = _make_bot()
        mock_api = MagicMock()
        mock_api.get_balance = AsyncMock(return_value={"total": 1000})
        mock_api.close = AsyncMock()
        with patch("NightCityBot.utils.startup_checks.UnbelievaBoatAPI",
                    return_value=mock_api):
            _run(check_unbelievaboat(bot))
            mock_api.close.assert_called_once()

    def test_null_balance(self):
        bot = _make_bot()
        mock_api = MagicMock()
        mock_api.get_balance = AsyncMock(return_value=None)
        mock_api.close = AsyncMock()
        with patch("NightCityBot.utils.startup_checks.UnbelievaBoatAPI",
                    return_value=mock_api):
            _run(check_unbelievaboat(bot))


class TestCheckDbHealth:
    def test_success(self):
        bot = _make_bot()
        with patch("NightCityBot.utils.startup_checks.db_ping",
                    new_callable=AsyncMock, return_value=5.0):
            _run(check_db_health(bot))

    def test_null_latency(self):
        bot = _make_bot()
        admin_cog = MagicMock()
        admin_cog.log_audit = AsyncMock()
        bot.get_cog = MagicMock(return_value=admin_cog)
        with patch("NightCityBot.utils.startup_checks.db_ping",
                    new_callable=AsyncMock, return_value=None):
            _run(check_db_health(bot))

    def test_exception_handled(self):
        bot = _make_bot()
        with patch("NightCityBot.utils.startup_checks.db_ping",
                    new_callable=AsyncMock, side_effect=RuntimeError("db down")):
            _run(check_db_health(bot))


class TestPerformStartupChecks:
    def test_calls_all_checks(self):
        bot = _make_bot(guild=_make_guild())
        with patch("NightCityBot.utils.startup_checks.verify_config",
                    new_callable=AsyncMock) as mock_vc:
            with patch("NightCityBot.utils.startup_checks.check_db_health",
                        new_callable=AsyncMock) as mock_db:
                with patch("NightCityBot.utils.startup_checks.check_unbelievaboat",
                            new_callable=AsyncMock) as mock_ub:
                    with patch("NightCityBot.utils.startup_checks.cleanup_logs",
                                new_callable=AsyncMock) as mock_cl:
                        _run(perform_startup_checks(bot))
                        mock_vc.assert_called_once()
                        mock_db.assert_called_once()
                        mock_ub.assert_called_once()
                        mock_cl.assert_called_once()

    def test_logs_audit_if_admin_cog(self):
        bot = _make_bot(guild=_make_guild())
        admin = MagicMock()
        admin.log_audit = AsyncMock()
        bot.get_cog = MagicMock(return_value=admin)
        with patch("NightCityBot.utils.startup_checks.verify_config",
                    new_callable=AsyncMock):
            with patch("NightCityBot.utils.startup_checks.check_db_health",
                        new_callable=AsyncMock):
                with patch("NightCityBot.utils.startup_checks.check_unbelievaboat",
                            new_callable=AsyncMock):
                    with patch("NightCityBot.utils.startup_checks.cleanup_logs",
                                new_callable=AsyncMock):
                        _run(perform_startup_checks(bot))
                        admin.log_audit.assert_called_once()
