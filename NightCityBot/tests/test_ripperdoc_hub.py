"""Tests for the ripperdoc hub panel buttons."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

import config
from NightCityBot.cogs.ripperdoc_hub import (
    RipperdocMenuView,
    _CheckupPatientSelectView,
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


def _make_interaction(user_id=100, guild_id=999):
    inter = MagicMock()
    inter.response = MagicMock()
    inter.response.defer = AsyncMock()
    inter.response.send_message = AsyncMock()
    inter.followup = MagicMock()
    inter.followup.send = AsyncMock()
    inter.guild = MagicMock()
    inter.guild.id = guild_id
    inter.user = MagicMock()
    inter.user.id = user_id
    inter.user.display_name = "DocRipper"
    inter.user.roles = []
    inter.client = MagicMock()
    inter.client.get_cog = MagicMock(return_value=None)
    return inter


def _make_member(mid, name, roles=None):
    m = MagicMock()
    m.id = mid
    m.display_name = name
    m.roles = roles or []
    m.guild_permissions = MagicMock()
    m.guild_permissions.administrator = False
    m.remove_roles = AsyncMock()
    return m


class TestCheckupButton:
    def test_checkup_button_exists(self):
        async def _test():
            view = RipperdocMenuView()
            btn = _find_button(view, "Checkup")
            assert btn is not None
            assert btn.custom_id == "ripperdoc:checkup"
        _run(_test())

    def test_checkup_cyberware_disabled(self):
        async def _test():
            view = RipperdocMenuView()
            inter = _make_interaction()
            control = MagicMock()
            control.is_enabled = MagicMock(return_value=False)
            inter.client.get_cog = MagicMock(side_effect=lambda n: control if n == "SystemControl" else None)
            member = _make_member(100, "DocRipper")
            rd_role = MagicMock()
            rd_role.id = config.RIPPERDOC_ROLE_ID
            member.roles = [rd_role]
            inter.user = member
            btn = _find_button(view, "Checkup")
            await btn.callback(inter)
            msg = inter.followup.send.call_args[0][0]
            assert "disabled" in msg.lower()
        _run(_test())

    def test_checkup_no_checkup_role_configured(self):
        async def _test():
            view = RipperdocMenuView()
            inter = _make_interaction()
            inter.guild.get_role = MagicMock(return_value=None)
            member = _make_member(100, "DocRipper")
            rd_role = MagicMock()
            rd_role.id = config.RIPPERDOC_ROLE_ID
            member.roles = [rd_role]
            inter.user = member
            btn = _find_button(view, "Checkup")
            await btn.callback(inter)
            msg = inter.followup.send.call_args[0][0]
            assert "not configured" in msg.lower()
        _run(_test())

    def test_checkup_shows_patient_picker(self):
        async def _test():
            view = RipperdocMenuView()
            inter = _make_interaction()
            checkup_role = MagicMock()
            checkup_role.id = config.CYBER_CHECKUP_ROLE_ID
            inter.guild.get_role = MagicMock(return_value=checkup_role)
            member = _make_member(100, "DocRipper")
            rd_role = MagicMock()
            rd_role.id = config.RIPPERDOC_ROLE_ID
            member.roles = [rd_role]
            inter.user = member
            btn = _find_button(view, "Checkup")
            await btn.callback(inter)
            kwargs = inter.followup.send.call_args.kwargs
            picker = kwargs["view"]
            assert isinstance(picker, _CheckupPatientSelectView)
        _run(_test())


class TestCheckupPatientSelectView:
    def test_patient_without_checkup_role(self):
        async def _test():
            view = _CheckupPatientSelectView(ripperdoc_id=100)
            inter = _make_interaction()
            checkup_role = MagicMock()
            checkup_role.id = config.CYBER_CHECKUP_ROLE_ID
            patient = _make_member(200, "Patient", roles=[])
            inter.guild.get_member = MagicMock(return_value=patient)
            inter.guild.get_role = MagicMock(return_value=checkup_role)
            select = view.children[0]
            select._values = [patient]
            type(select).values = property(lambda self: self._values)
            await select.callback(inter)
            msg = inter.followup.send.call_args[0][0]
            assert "does not have" in msg.lower()
        _run(_test())

    def test_patient_checkup_success(self):
        async def _test():
            view = _CheckupPatientSelectView(ripperdoc_id=100)
            inter = _make_interaction()
            checkup_role = MagicMock()
            checkup_role.id = config.CYBER_CHECKUP_ROLE_ID
            patient = _make_member(200, "Patient", roles=[checkup_role])
            inter.guild.get_member = MagicMock(return_value=patient)
            inter.guild.get_role = MagicMock(return_value=checkup_role)
            log_channel = MagicMock()
            log_channel.send = AsyncMock()
            inter.guild.get_channel = MagicMock(return_value=log_channel)
            inter.client.get_cog = MagicMock(return_value=None)
            select = view.children[0]
            select._values = [patient]
            type(select).values = property(lambda self: self._values)
            with patch("NightCityBot.utils.db.cyberware_status_upsert", new_callable=AsyncMock) as mock_upsert:
                await select.callback(inter)
            msg = inter.followup.send.call_args[0][0]
            assert "removed checkup role" in msg.lower()
            patient.remove_roles.assert_called_once()
            mock_upsert.assert_awaited_once_with("200", 0, None)
        _run(_test())

    def test_interaction_check_wrong_user(self):
        async def _test():
            view = _CheckupPatientSelectView(ripperdoc_id=100)
            inter = _make_interaction(user_id=999)
            result = await view.interaction_check(inter)
            assert result is False
        _run(_test())

    def test_interaction_check_correct_user(self):
        async def _test():
            view = _CheckupPatientSelectView(ripperdoc_id=100)
            inter = _make_interaction(user_id=100)
            result = await view.interaction_check(inter)
            assert result is True
        _run(_test())
