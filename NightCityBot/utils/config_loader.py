"""In-memory cache for bot_config DB rows with hardcoded fallback defaults.

Usage
-----
    from NightCityBot.utils import config_loader as cfg

    baseline = cfg.get_baseline_living_cost()   # int
    tiers    = cfg.get_role_costs_business()     # dict[str, int]

Call ``await cfg.seed_and_reload()`` once at bot startup to populate the
cache from the database.  The cache is also refreshed whenever an admin
runs ``!reload_config``.

All getter functions fall back to the hardcoded defaults below when the
database is unreachable or the key has not been seeded yet, so the bot
remains fully functional even without DB access.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Hardcoded defaults (these are the "source of truth" fallbacks)
# ---------------------------------------------------------------------------

_DEFAULTS: dict[str, tuple[Any, str]] = {
    # Rent / living costs
    "baseline_living_cost":   (500,   "Monthly baseline cost charged to all non-LOA players"),
    "business_tier_0_rent":   (0,     "Monthly rent for Business Tier 0"),
    "business_tier_1_rent":   (2000,  "Monthly rent for Business Tier 1"),
    "business_tier_2_rent":   (3000,  "Monthly rent for Business Tier 2"),
    "business_tier_3_rent":   (5000,  "Monthly rent for Business Tier 3"),
    "housing_tier_1_rent":    (1000,  "Monthly rent for Housing Tier 1"),
    "housing_tier_2_rent":    (2000,  "Monthly rent for Housing Tier 2"),
    "housing_tier_3_rent":    (3000,  "Monthly rent for Housing Tier 3"),
    # Trauma Team subscriptions
    "trauma_silver_cost":     (1000,  "Monthly Trauma Team Silver subscription"),
    "trauma_gold_cost":       (2000,  "Monthly Trauma Team Gold subscription"),
    "trauma_plat_cost":       (4000,  "Monthly Trauma Team Plat subscription"),
    "trauma_diamond_cost":    (10000, "Monthly Trauma Team Diamond subscription"),
    # Passive income (Tier 0 flat scale, 1-4 opens)
    "tier0_income_1_open":    (150,   "Tier-0 passive income for 1 business open/month"),
    "tier0_income_2_open":    (250,   "Tier-0 passive income for 2 business opens/month"),
    "tier0_income_3_open":    (350,   "Tier-0 passive income for 3 business opens/month"),
    "tier0_income_4_open":    (500,   "Tier-0 passive income for 4 business opens/month"),
    # Passive income (Tiers 1-3 percentage, 1-4 opens; stored as integer percent, e.g. 25 = 25%)
    "open_percent_1":         (25,    "Passive income percentage of base rent for 1 open/month"),
    "open_percent_2":         (40,    "Passive income percentage of base rent for 2 opens/month"),
    "open_percent_3":         (60,    "Passive income percentage of base rent for 3 opens/month"),
    "open_percent_4":         (80,    "Passive income percentage of base rent for 4 opens/month"),
    # Attendance reward
    "attend_reward":          (250,   "Cash reward for !attend each Sunday"),
    # Cyberware medication caps
    "cyber_max_cost_medium":  (2000,  "Maximum weekly cyberware medication cost for Medium level"),
    "cyber_max_cost_high":    (5000,  "Maximum weekly cyberware medication cost for High level"),
    "cyber_max_cost_extreme": (10000, "Maximum weekly cyberware medication cost for Extreme level"),
}


def _default_value(key: str) -> Any:
    return _DEFAULTS[key][0]


# ---------------------------------------------------------------------------
# In-memory cache (populated from DB at startup and on !reload_config)
# ---------------------------------------------------------------------------

_cache: dict[str, str] = {}


def _cfg_int(key: str) -> int:
    """Return cached value as int, falling back to hardcoded default."""
    raw = _cache.get(key)
    if raw is not None:
        try:
            return int(raw)
        except (ValueError, TypeError):
            pass
    return int(_default_value(key))


def _cfg_float(key: str) -> float:
    """Return cached value as float, falling back to hardcoded default."""
    raw = _cache.get(key)
    if raw is not None:
        try:
            return float(raw)
        except (ValueError, TypeError):
            pass
    return float(_default_value(key))


# ---------------------------------------------------------------------------
# Public accessor functions — same shape as the old constants
# ---------------------------------------------------------------------------

def get_baseline_living_cost() -> int:
    return _cfg_int("baseline_living_cost")


def get_attend_reward() -> int:
    return _cfg_int("attend_reward")


def get_role_costs_business() -> dict[str, int]:
    return {
        "Business Tier 0": _cfg_int("business_tier_0_rent"),
        "Business Tier 1": _cfg_int("business_tier_1_rent"),
        "Business Tier 2": _cfg_int("business_tier_2_rent"),
        "Business Tier 3": _cfg_int("business_tier_3_rent"),
    }


def get_role_costs_housing() -> dict[str, int]:
    return {
        "Housing Tier 1": _cfg_int("housing_tier_1_rent"),
        "Housing Tier 2": _cfg_int("housing_tier_2_rent"),
        "Housing Tier 3": _cfg_int("housing_tier_3_rent"),
    }


def get_trauma_role_costs() -> dict[str, int]:
    return {
        "Trauma Team Silver":  _cfg_int("trauma_silver_cost"),
        "Trauma Team Gold":    _cfg_int("trauma_gold_cost"),
        "Trauma Team Plat":    _cfg_int("trauma_plat_cost"),
        "Trauma Team Diamond": _cfg_int("trauma_diamond_cost"),
    }


def get_tier0_income_scale() -> dict[int, int]:
    return {
        1: _cfg_int("tier0_income_1_open"),
        2: _cfg_int("tier0_income_2_open"),
        3: _cfg_int("tier0_income_3_open"),
        4: _cfg_int("tier0_income_4_open"),
    }


def get_open_percent() -> dict[int, float]:
    return {
        0: 0.0,
        1: _cfg_int("open_percent_1") / 100.0,
        2: _cfg_int("open_percent_2") / 100.0,
        3: _cfg_int("open_percent_3") / 100.0,
        4: _cfg_int("open_percent_4") / 100.0,
    }


def get_cyber_max_cost() -> dict[str, int]:
    return {
        "medium":  _cfg_int("cyber_max_cost_medium"),
        "high":    _cfg_int("cyber_max_cost_high"),
        "extreme": _cfg_int("cyber_max_cost_extreme"),
    }


# ---------------------------------------------------------------------------
# Lifecycle helpers called at startup and by !reload_config
# ---------------------------------------------------------------------------

async def seed_and_reload() -> None:
    """Seed missing DB defaults then populate the in-memory cache."""
    from NightCityBot.utils.db import bot_config_seed, bot_config_get_all
    await bot_config_seed(_DEFAULTS)
    await _reload_cache()


async def _reload_cache() -> None:
    """Fetch all bot_config rows from the DB and update the in-memory cache."""
    from NightCityBot.utils.db import bot_config_get_all
    rows = await bot_config_get_all()
    _cache.clear()
    for key, value, _desc in rows:
        _cache[key] = value
    logger.info("config_loader: cache refreshed (%d keys)", len(_cache))


async def reload_config() -> None:
    """Public alias used by the !reload_config admin command."""
    await _reload_cache()


def get_all_defaults() -> dict[str, tuple[Any, str]]:
    """Return the full defaults dict (key → (default_value, description))."""
    return dict(_DEFAULTS)


def get_cache_snapshot() -> dict[str, str]:
    """Return a copy of the current in-memory cache."""
    return dict(_cache)
