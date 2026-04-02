import discord
from pathlib import Path
from typing import Iterable
import logging

logger = logging.getLogger(__name__)

import config
from .helpers import load_json_file, save_json_file
from NightCityBot.services.unbelievaboat import UnbelievaBoatAPI
from NightCityBot.utils.db import db_ping

# Role and channel identifiers to verify
ROLE_ID_FIELDS: Iterable[str] = [
    "FIXER_ROLE_ID",
    "TRAUMA_TEAM_ROLE_ID",
    "VERIFIED_ROLE_ID",
    "NPC_ROLE_ID",
    "CYBER_CHECKUP_ROLE_ID",
    "CYBER_MEDIUM_ROLE_ID",
    "CYBER_HIGH_ROLE_ID",
    "CYBER_EXTREME_ROLE_ID",
    "LOA_ROLE_ID",
    "RIPPERDOC_ROLE_ID",
]

CHANNEL_ID_FIELDS: Iterable[str] = [
    "DM_INBOX_CHANNEL_ID",
    "BUSINESS_ACTIVITY_CHANNEL_ID",
    "RENT_LOG_CHANNEL_ID",
    "EVICTION_CHANNEL_ID",
    "TRAUMA_NOTIFICATIONS_CHANNEL_ID",
    "AUDIT_LOG_CHANNEL_ID",
    "GROUP_AUDIT_LOG_CHANNEL_ID",
    "NIGHTCITYBOT_LOG_CHANNEL_ID",
    "GUN_LOG_CHANNEL_ID",
    "CYBERWARE_LOG_CHANNEL_ID",
    "GEAR_MISC_LOG_CHANNEL_ID",
    "RIPPERDOC_LOG_CHANNEL_ID",
]

# Channels that must exist AND be Discord ForumChannels.
FORUM_CHANNEL_ID_FIELDS: Iterable[str] = [
    "TRAUMA_FORUM_CHANNEL_ID",
    "RP_LOG_FORUM_CHANNEL_ID",
    "CHARACTER_SHEETS_CHANNEL_ID",
    "RETIRED_SHEETS_CHANNEL_ID",
    "NPC_SHEETS_CHANNEL_ID",
]

# Additional config values that should not be empty
REQUIRED_FIELDS: Iterable[str] = [
    "AUDIT_LOG_CHANNEL_ID",
    "GROUP_AUDIT_LOG_CHANNEL_ID",
    "FIXER_ROLE_NAME",
    "FIXER_ROLE_ID",
    "DM_INBOX_CHANNEL_ID",
    "GUILD_ID",
    "TEST_USER_ID",
    "REPORT_USER_ID",
    "BUSINESS_ACTIVITY_CHANNEL_ID",
    "ATTENDANCE_CHANNEL_ID",
    "RENT_LOG_CHANNEL_ID",
    "EVICTION_CHANNEL_ID",
    "TRAUMA_TEAM_ROLE_ID",
    "TRAUMA_FORUM_CHANNEL_ID",
    "TRAUMA_NOTIFICATIONS_CHANNEL_ID",
    "VERIFIED_ROLE_ID",
    "APPROVED_ROLE_ID",
    "NPC_ROLE_ID",
    "THREAD_MAP_FILE",
    "OPEN_LOG_FILE",
    "LAST_RENT_FILE",
    "LAST_PAYMENT_FILE",
    "BALANCE_BACKUP_DIR",
    "CHARACTER_BACKUP_DIR",
    "RENT_AUDIT_DIR",
    "ATTEND_LOG_FILE",
    "CYBERWARE_LOG_FILE",
    "CYBERWARE_WEEKLY_FILE",
    "SYSTEM_STATUS_FILE",
    "CYBER_CHECKUP_ROLE_ID",
    "CYBER_MEDIUM_ROLE_ID",
    "CYBER_HIGH_ROLE_ID",
    "CYBER_EXTREME_ROLE_ID",
    "LOA_ROLE_ID",
    "RIPPERDOC_ROLE_ID",
    "RIPPERDOC_LOG_CHANNEL_ID",
    "NIGHTCITYBOT_LOG_CHANNEL_ID",
    "GUN_LOG_CHANNEL_ID",
    "CYBERWARE_LOG_CHANNEL_ID",
    "GEAR_MISC_LOG_CHANNEL_ID",
    "TIMEZONE",
    "RP_IC_CATEGORY_ID",
    "CHARACTER_SHEETS_CHANNEL_ID",
    "RETIRED_SHEETS_CHANNEL_ID",
    "NPC_SHEETS_CHANNEL_ID",
]

LOG_FILES = [
    config.THREAD_MAP_FILE,
    config.OPEN_LOG_FILE,
    config.ATTEND_LOG_FILE,
    config.CYBERWARE_LOG_FILE,
]

async def verify_config(bot: discord.Client) -> None:
    guild = bot.get_guild(config.GUILD_ID)
    if not guild:
        logger.warning("\u26a0\ufe0f Guild with ID %s not found.", config.GUILD_ID)
        return

    issues = False
    # Ensure required config values are populated
    for field in REQUIRED_FIELDS:
        val = getattr(config, field, None)
        logger.debug("Checking value %s: %s", field, val)
        if val in (None, "", 0):
            logger.warning("\u26a0\ufe0f Missing value for %s", field)
            issues = True
    for field in ROLE_ID_FIELDS:
        role_id = getattr(config, field, 0)
        logger.debug("Checking role %s: %s", field, role_id)
        if role_id and guild.get_role(role_id) is None:
            logger.warning("\u26a0\ufe0f Missing role for %s: %s", field, role_id)
            issues = True

    # Check that configured channels exist
    for field in CHANNEL_ID_FIELDS:
        ch_id = getattr(config, field, 0)
        logger.debug("Checking channel %s: %s", field, ch_id)
        if ch_id and guild.get_channel(ch_id) is None:
            logger.warning("\u26a0\ufe0f Missing channel for %s: %s", field, ch_id)
            issues = True

    # Check that channels expected to be ForumChannels exist AND are the right type.
    for field in FORUM_CHANNEL_ID_FIELDS:
        ch_id = getattr(config, field, 0)
        logger.debug("Checking forum channel %s: %s", field, ch_id)
        if not ch_id:
            logger.warning("\u26a0\ufe0f %s is not configured (required ForumChannel)", field)
            issues = True
        else:
            ch = guild.get_channel(ch_id)
            if ch is None:
                logger.warning("\u26a0\ufe0f Channel not found for %s: %s", field, ch_id)
                issues = True
            elif not isinstance(ch, discord.ForumChannel):
                logger.warning(
                    "\u26a0\ufe0f %s (id=%s) is %s, expected ForumChannel — "
                    "commands that depend on this will fail.",
                    field, ch_id, type(ch).__name__,
                )
                issues = True

    # Check bot permissions
    required_perms = [
        "send_messages",
        "manage_messages",
        "manage_channels",
        "manage_roles",
        "attach_files",
        "embed_links",
    ]
    me = guild.me
    for perm in required_perms:
        logger.debug("Checking permission: %s", perm)
        if not getattr(me.guild_permissions, perm, False):
            logger.warning("\u26a0\ufe0f Bot missing permission: %s", perm)
            issues = True

    hour = getattr(config, "RENT_COLLECTION_HOUR", 0)
    minute = getattr(config, "RENT_COLLECTION_MINUTE", 0)
    if not isinstance(hour, int) or not (0 <= hour <= 23):
        logger.warning("⚠️ RENT_COLLECTION_HOUR must be an integer 0–23 (got %r)", hour)
        issues = True
    if not isinstance(minute, int) or not (0 <= minute <= 59):
        logger.warning("⚠️ RENT_COLLECTION_MINUTE must be an integer 0–59 (got %r)", minute)
        issues = True

    if not issues:
        logger.info("\u2705 Configuration verified with no issues.")

async def cleanup_logs(bot: discord.Client) -> None:
    guild = bot.get_guild(config.GUILD_ID)
    if not guild:
        return

    member_ids = {str(m.id) for m in guild.members}

    for file_path in LOG_FILES:
        path = Path(file_path)
        if not path.exists():
            continue
        data = await load_json_file(path, default={})
        cleaned = {uid: val for uid, val in data.items() if uid in member_ids}
        if cleaned != data:
            await save_json_file(path, cleaned)
            logger.info("\u2705 Cleaned orphaned entries from %s", path.name)

async def check_unbelievaboat(bot: discord.Client) -> None:
    """Verify we can reach the UnbelievaBoat API."""
    token = getattr(config, "UNBELIEVABOAT_API_TOKEN", None)
    if not token:
        logger.warning("\u26a0\ufe0f UNBELIEVABOAT_API_TOKEN not configured.")
        return
    api = UnbelievaBoatAPI(token)
    try:
        logger.info("Checking UnbelievaBoat connection...")
        result = await api.get_balance(getattr(config, "TEST_USER_ID", 0))
        if result is not None:
            logger.info("\u2705 Connected to UnbelievaBoat successfully.")
        else:
            logger.warning("\u26a0\ufe0f Failed to fetch balance from UnbelievaBoat.")
    finally:
        await api.close()

async def check_db_health(bot: discord.Client) -> None:
    """Verify the PostgreSQL connection with SELECT 1 and alert on failure."""
    try:
        latency = await db_ping()
        if latency is None:
            logger.critical("\u274c DB startup health check FAILED — could not connect.")
            admin = bot.get_cog("Admin")
            if admin:
                await admin.log_audit(
                    bot.user,
                    "\U0001f534 **DB startup health check FAILED** — SELECT 1 returned no result. "
                    "Write operations may be unreliable.",
                )
        else:
            logger.info("\u2705 DB health check OK (%.1f ms)", latency)
    except Exception:
        logger.error("check_db_health raised unexpectedly", exc_info=True)


async def perform_startup_checks(bot: discord.Client) -> None:
    await bot.wait_until_ready()
    await verify_config(bot)
    await check_db_health(bot)
    await check_unbelievaboat(bot)
    await cleanup_logs(bot)
    admin = bot.get_cog('Admin')
    if admin:
        await admin.log_audit(bot.user, "✅ Bot successfully started.")
    logger.info("\u2705 Bot successfully started and ready.")
