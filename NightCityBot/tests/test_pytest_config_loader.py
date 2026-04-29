"""
Pytest tests verifying that config_loader getters return correct defaults
and that economy/cyberware calculations read values from the config cache.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import discord
from discord.ext import commands

import config
from NightCityBot.utils import config_loader as _cfg
from NightCityBot.utils.constants import (
    BASELINE_LIVING_COST,
    ATTEND_REWARD,
    ROLE_COSTS_BUSINESS,
    ROLE_COSTS_HOUSING,
    TRAUMA_ROLE_COSTS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class DummyBot:
    def __init__(self):
        self.cogs = {}
        self.loop = asyncio.new_event_loop()
        self.unbelievaboat = MagicMock()

    def add_cog(self, cog):
        self.cogs[cog.__class__.__name__] = cog
        for attr in dir(cog):
            cmd = getattr(cog, attr)
            if isinstance(cmd, commands.Command):
                cmd.cog = cog

    def get_cog(self, name):
        return self.cogs.get(name)


def _make_economy():
    from NightCityBot.cogs.economy import Economy
    bot = DummyBot()
    with patch("NightCityBot.services.unbelievaboat.aiohttp.ClientSession", new=MagicMock()):
        econ = Economy(bot)
    bot.add_cog(econ)
    return econ


def _make_cyberware():
    from NightCityBot.cogs.cyberware import CyberwareManager
    bot = DummyBot()
    with (
        patch("NightCityBot.services.unbelievaboat.aiohttp.ClientSession", new=MagicMock()),
        patch("asyncio.create_task", lambda *a, **k: None),
    ):
        cyber = CyberwareManager(bot)
    bot.add_cog(cyber)
    return cyber


# ---------------------------------------------------------------------------
# Config-loader default value tests
# ---------------------------------------------------------------------------

def test_default_baseline_living_cost():
    """get_baseline_living_cost() returns the hardcoded default when cache is empty."""
    with patch.dict(_cfg._cache, {}, clear=True):
        assert _cfg.get_baseline_living_cost() == BASELINE_LIVING_COST


def test_default_attend_reward():
    """get_attend_reward() returns the hardcoded default when cache is empty."""
    with patch.dict(_cfg._cache, {}, clear=True):
        assert _cfg.get_attend_reward() == ATTEND_REWARD


def test_default_role_costs_business():
    """get_role_costs_business() returns the same mapping as the constant."""
    with patch.dict(_cfg._cache, {}, clear=True):
        assert _cfg.get_role_costs_business() == ROLE_COSTS_BUSINESS


def test_default_role_costs_housing():
    """get_role_costs_housing() returns the same mapping as the constant."""
    with patch.dict(_cfg._cache, {}, clear=True):
        assert _cfg.get_role_costs_housing() == ROLE_COSTS_HOUSING


def test_default_trauma_role_costs():
    """get_trauma_role_costs() returns the same mapping as the constant."""
    with patch.dict(_cfg._cache, {}, clear=True):
        assert _cfg.get_trauma_role_costs() == TRAUMA_ROLE_COSTS


def test_default_xanadu_gold_cost():
    """get_xanadu_gold_cost() returns the seeded $500 default."""
    with patch.dict(_cfg._cache, {}, clear=True):
        assert _cfg.get_xanadu_gold_cost() == 500


def test_xanadu_gold_cost_override():
    """get_xanadu_gold_cost() respects the in-memory cache override."""
    with patch.dict(_cfg._cache, {"xanadu_gold_cost": "750"}, clear=True):
        assert _cfg.get_xanadu_gold_cost() == 750


def test_default_cyber_max_cost():
    """get_cyber_max_cost() returns expected defaults for medium/high/extreme."""
    with patch.dict(_cfg._cache, {}, clear=True):
        mc = _cfg.get_cyber_max_cost()
    assert mc["medium"] == 2000
    assert mc["high"] == 5000
    assert mc["extreme"] == 10000


# ---------------------------------------------------------------------------
# Overriding via cache
# ---------------------------------------------------------------------------

def test_baseline_override_via_cache():
    """Setting baseline_living_cost in cache changes what the getter returns."""
    with patch.dict(_cfg._cache, {"baseline_living_cost": "999"}, clear=False):
        assert _cfg.get_baseline_living_cost() == 999


def test_attend_reward_override_via_cache():
    with patch.dict(_cfg._cache, {"attend_reward": "200"}, clear=False):
        assert _cfg.get_attend_reward() == 200


# ---------------------------------------------------------------------------
# Cyberware calculate_cost reads from config_loader
# ---------------------------------------------------------------------------

def test_cyberware_calculate_cost_defaults():
    """calculate_cost uses config_loader defaults when cache is empty."""
    cyber = _make_cyberware()
    with patch.dict(_cfg._cache, {}, clear=True):
        cost_w1 = cyber.calculate_cost("medium", 1)
        cost_cap = cyber.calculate_cost("extreme", 8)
    assert cost_w1 == int(2000 / 128 * 1)
    assert cost_cap == 10000


def test_cyberware_calculate_cost_respects_override():
    """calculate_cost uses the overridden max cost from the DB cache."""
    cyber = _make_cyberware()
    # Override extreme max cost to 20000
    override_cache = {
        "cyber_max_cost_extreme": "20000",
        "cyber_max_cost_medium": "2000",
        "cyber_max_cost_high": "5000",
    }
    with patch.dict(_cfg._cache, override_cache, clear=False):
        cost_cap = cyber.calculate_cost("extreme", 8)
    assert cost_cap == 20000


# ---------------------------------------------------------------------------
# Economy calculate_due reads from config_loader
# ---------------------------------------------------------------------------

def _make_member_with_roles(*role_names):
    guild = MagicMock()
    loa_role = MagicMock(spec=discord.Role)
    loa_role.id = config.LOA_ROLE_ID
    guild.get_role.return_value = loa_role

    roles = []
    for name in role_names:
        r = MagicMock(spec=discord.Role)
        r.name = name
        r.id = 9999
        roles.append(r)

    member = MagicMock(spec=discord.Member)
    member.roles = roles
    member.guild = guild
    return member


def test_calculate_due_uses_baseline_from_cache():
    """calculate_due() picks up baseline_living_cost from config cache."""
    econ = _make_economy()
    member = _make_member_with_roles()  # no tier roles, no LOA

    with patch.dict(_cfg._cache, {"baseline_living_cost": "750"}, clear=False):
        total, _ = econ.calculate_due(member)

    assert total >= 750, f"Expected baseline ≥ 750, got {total}"


def test_calculate_due_uses_housing_cost_from_cache():
    """calculate_due() picks up housing tier cost from config cache."""
    econ = _make_economy()
    member = _make_member_with_roles("Housing Tier 1")

    baseline = _cfg.get_baseline_living_cost()
    with patch.dict(_cfg._cache, {"housing_tier_1_rent": "300"}, clear=False):
        total, _ = econ.calculate_due(member)

    assert total >= baseline + 300, f"Expected at least baseline+300, got {total}"


# ---------------------------------------------------------------------------
# Economy.calculate_monthly_due — preview helper for new player-hub button
# ---------------------------------------------------------------------------

def test_calculate_monthly_due_excludes_cyberware():
    """calculate_monthly_due returns baseline+housing+business+trauma but no cyber meds."""
    econ = _make_economy()
    member = _make_member_with_roles("Housing Tier 1")

    with patch.dict(_cfg._cache, {
        "baseline_living_cost": "500",
        "housing_tier_1_rent": "200",
    }, clear=False):
        total, details = econ.calculate_monthly_due(member)

    assert total >= 700, f"Expected ≥700 from baseline+housing, got {total}"
    joined = " | ".join(details)
    assert "Cyberware" not in joined, f"monthly preview must exclude cyber meds: {joined}"


def test_calculate_monthly_due_loa_skips_baseline():
    """LOA members skip baseline and housing in the monthly preview."""
    econ = _make_economy()

    guild = MagicMock()
    loa_role = MagicMock(spec=discord.Role)
    loa_role.id = config.LOA_ROLE_ID
    guild.get_role.return_value = loa_role
    member = MagicMock(spec=discord.Member)
    member.roles = [loa_role]
    member.guild = guild

    total, details = econ.calculate_monthly_due(member)
    assert total == 0
    assert any("LOA" in d for d in details)


# ---------------------------------------------------------------------------
# CyberwareManager.preview_weekly_cost — preview helper for new hub button
# ---------------------------------------------------------------------------

def _make_member_for_cyber(role_ids):
    """Build a member whose .roles contain mocked discord.Role objects with the
    given config IDs, plus a guild whose .get_role(rid) returns each role
    when its id matches.
    """
    roles = []
    for rid in role_ids:
        r = MagicMock(spec=discord.Role)
        r.id = rid
        r.name = f"role-{rid}"
        roles.append(r)

    guild = MagicMock()

    def _get_role(rid):
        for r in roles:
            if r.id == rid:
                return r
        stub = MagicMock(spec=discord.Role)
        stub.id = rid
        stub.name = f"stub-{rid}"
        return stub

    guild.get_role.side_effect = _get_role
    member = MagicMock(spec=discord.Member)
    member.roles = roles
    member.guild = guild
    member.id = 12345
    return member


def test_preview_weekly_cost_no_cyber_role_returns_none():
    cyber = _make_cyberware()
    member = _make_member_for_cyber([])  # no cyberware roles
    assert cyber.preview_weekly_cost(member) is None


def test_preview_weekly_cost_loa_returns_none():
    cyber = _make_cyberware()
    member = _make_member_for_cyber([
        config.LOA_ROLE_ID,
        config.CYBER_HIGH_ROLE_ID,
    ])
    assert cyber.preview_weekly_cost(member) is None


def test_preview_weekly_cost_ripperdoc_returns_none():
    cyber = _make_cyberware()
    member = _make_member_for_cyber([
        config.RIPPERDOC_ROLE_ID,
        config.CYBER_HIGH_ROLE_ID,
    ])
    assert cyber.preview_weekly_cost(member) is None


def test_preview_weekly_cost_no_checkup_returns_zero_cost():
    cyber = _make_cyberware()
    member = _make_member_for_cyber([config.CYBER_MEDIUM_ROLE_ID])
    cyber.data = {}
    out = cyber.preview_weekly_cost(member)
    assert out is not None
    assert out["level"] == "medium"
    assert out["has_checkup"] is False
    # Immediate Monday charge is $0 — they'll just get the role assigned
    assert out["cost"] == 0
    assert out["upcoming_weeks"] == 0
    # But the projected next charge (one cycle later) is non-zero so the
    # player can see what they'll eventually owe if they keep ignoring.
    assert out["next_charge_weeks"] == 1
    assert out["next_charge_cost"] == cyber.calculate_cost("medium", 1)
    assert out["next_charge_cost"] > 0


def test_preview_weekly_cost_with_checkup_charges_next_streak():
    """When member has the checkup role, cost should match calculate_cost(level, streak+1)."""
    cyber = _make_cyberware()
    member = _make_member_for_cyber([
        config.CYBER_HIGH_ROLE_ID,
        config.CYBER_CHECKUP_ROLE_ID,
    ])
    cyber.data = {str(member.id): {"weeks": 2, "last": None}}

    out = cyber.preview_weekly_cost(member)
    assert out is not None
    assert out["level"] == "high"
    assert out["has_checkup"] is True
    assert out["current_streak"] == 2
    assert out["upcoming_weeks"] == 3
    assert out["cost"] == cyber.calculate_cost("high", 3)
    # When already flagged, next_charge_* mirrors the immediate charge.
    assert out["next_charge_cost"] == out["cost"]
    assert out["next_charge_weeks"] == out["upcoming_weeks"]


def test_preview_weekly_cost_works_when_guild_get_role_returns_none():
    """Regression: a high-cyber player should still be detected even when
    `guild.get_role()` returns None for cached lookups (which can happen in
    interaction contexts where the role cache isn't fully populated). The
    member object's role IDs are the source of truth.
    """
    cyber = _make_cyberware()

    role = MagicMock(spec=discord.Role)
    role.id = config.CYBER_HIGH_ROLE_ID
    role.name = "High Cyberware"

    guild = MagicMock()
    guild.get_role = MagicMock(return_value=None)  # role cache "empty"

    member = MagicMock(spec=discord.Member)
    member.id = 99999
    member.roles = [role]
    member.guild = guild

    cyber.data = {}
    out = cyber.preview_weekly_cost(member)

    assert out is not None, "should detect High cyber via role IDs even when get_role returns None"
    assert out["level"] == "high"
    assert out["next_charge_cost"] > 0


def test_preview_weekly_cost_extreme_takes_precedence():
    """If member has multiple cyber-tier roles, extreme wins over high/medium."""
    cyber = _make_cyberware()
    member = _make_member_for_cyber([
        config.CYBER_MEDIUM_ROLE_ID,
        config.CYBER_HIGH_ROLE_ID,
        config.CYBER_EXTREME_ROLE_ID,
    ])
    cyber.data = {}
    out = cyber.preview_weekly_cost(member)
    assert out is not None
    assert out["level"] == "extreme"
