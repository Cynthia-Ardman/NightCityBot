import discord
from discord.ext import commands

import os
import signal
import asyncio
import fcntl
import sys
import logging

package_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if package_root not in sys.path:
    sys.path.insert(0, package_root)

import config

from NightCityBot.utils.permissions import is_fixer
from NightCityBot.cogs.dm_handling import DMHandler
from NightCityBot.cogs.economy import Economy
from NightCityBot.cogs.rp_manager import RPManager
from NightCityBot.cogs.roll_system import RollSystem
from NightCityBot.cogs.admin import Admin
from NightCityBot.cogs.test_suite import TestSuite
from NightCityBot.cogs.cyberware import CyberwareManager
from NightCityBot.cogs.loa import LOA
from NightCityBot.cogs.character_manager import CharacterManager
from NightCityBot.cogs.system_control import SystemControl
from NightCityBot.cogs.role_buttons import RoleButtons
from NightCityBot.cogs.trauma_team import TraumaTeam
from NightCityBot.cogs.guns_shop import GunsShopCog
from NightCityBot.cogs.cyberware_shop import CyberwareShop
from NightCityBot.cogs.player_inventory import PlayerInventoryCog
from NightCityBot.cogs.ripperdoc_hub import RipperdocHub
from NightCityBot.cogs.gunstore_hub import GunstoreHub
from NightCityBot.cogs.admin_shop import AdminShopCog
from NightCityBot.cogs.backup import Backup
from NightCityBot.utils.startup_checks import perform_startup_checks
from NightCityBot.services.unbelievaboat import UnbelievaBoatAPI
from NightCityBot.utils import config_loader as _cfg

from flask import Flask
from threading import Thread

logger = logging.getLogger(__name__)


class NightCityBot(commands.Bot):
    """Discord bot wrapper for NCRP."""

    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.messages = True
        intents.message_content = True
        intents.members = True
        intents.dm_messages = True

        super().__init__(command_prefix="!", help_command=None, intents=intents)
        self._shutdown_logged = False
        self.unbelievaboat: UnbelievaBoatAPI | None = None

    async def setup_hook(self):
        self.unbelievaboat = UnbelievaBoatAPI(config.UNBELIEVABOAT_API_TOKEN)
        # Seed + load bot_config cache before cogs start so constants are ready
        try:
            await _cfg.seed_and_reload()
        except Exception:
            logger.warning("bot_config seed/reload failed at startup — using hardcoded defaults", exc_info=True)
        await self.add_cog(DMHandler(self))
        await self.add_cog(SystemControl(self))
        await self.add_cog(Economy(self))
        await self.add_cog(RPManager(self))
        await self.add_cog(RollSystem(self))
        await self.add_cog(CyberwareManager(self))
        await self.add_cog(LOA(self))
        await self.add_cog(CharacterManager(self))
        await self.add_cog(RoleButtons(self))
        await self.add_cog(TraumaTeam(self))
        await self.add_cog(GunsShopCog(self))
        await self.add_cog(CyberwareShop(self))
        await self.add_cog(PlayerInventoryCog(self, self.unbelievaboat))
        await self.add_cog(RipperdocHub(self))
        await self.add_cog(GunstoreHub(self))
        await self.add_cog(AdminShopCog(self))
        await self.add_cog(Backup(self))
        await self.add_cog(Admin(self))
        await self.add_cog(TestSuite(self))
        self.loop.create_task(perform_startup_checks(self))

    async def on_message(self, message: discord.Message):
        if message.author == self.user or message.author.bot:
            return
        dm_handler = self.get_cog("DMHandler")
        if dm_handler and isinstance(message.channel, discord.Thread):
            if message.channel.id in getattr(dm_handler, "dm_threads", {}).values():
                return

        if isinstance(
            message.channel, discord.TextChannel
        ) and message.channel.name.startswith("text-rp-"):
            # Still process commands (e.g. !end_rp) — the RPManager cog
            # listener handles deleting the command message after a short delay.
            if message.content.strip().startswith(self.command_prefix):
                await self.process_commands(message)
            return

        await self.process_commands(message)

    async def on_ready(self):
        logger.info("%s is running!", self.user.name)
        admin = self.get_cog("Admin")
        if admin:
            await admin.log_audit(self.user, "✅ Bot started and ready.")

    async def close(self):
        if not self._shutdown_logged:
            self._shutdown_logged = True
            admin = self.get_cog("Admin")
            if admin and self.user:
                try:
                    await admin.log_audit(self.user, "🛑 Bot shutting down.")
                except Exception:
                    logger.exception("Failed to log shutdown audit")
        if self.unbelievaboat is not None:
            await self.unbelievaboat.close()
        await super().close()


app = Flask("")


@app.route("/")
def home():
    return "Bot is alive Version 1.2!", 200


@app.route("/healthz")
def healthz():
    from flask import jsonify
    return jsonify({"status": "ok"}), 200


@app.route("/readyz")
def readyz():
    from flask import jsonify
    return jsonify({"status": "ready"}), 200


def _resolve_keep_alive_port() -> int:
    raw_port = str(os.getenv("PORT", "5000")).strip()
    try:
        return int(raw_port)
    except ValueError:
        logger.warning("Invalid PORT value '%s'; falling back to 5000", raw_port)
        return 5000


def run_flask():
    log = logging.getLogger("werkzeug")
    log.setLevel(logging.WARNING)
    app.run(
        host="0.0.0.0",
        port=_resolve_keep_alive_port(),
        debug=False,
        use_reloader=False,
    )


def keep_alive() -> bool:
    """Start the optional keep-alive HTTP server."""
    disabled = os.getenv("DISABLE_KEEP_ALIVE", "").lower() in {"1", "true", "yes"}
    if disabled:
        logger.info("Skipping keep-alive server (DISABLE_KEEP_ALIVE=true)")
        return False

    t = Thread(target=run_flask, daemon=True)
    t.start()
    return True


_lock_file = None


def acquire_instance_lock() -> bool:
    """Ensure only one bot instance runs at a time using a file lock."""
    global _lock_file
    lock_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), ".bot.lock"
    )
    try:
        _lock_file = open(lock_path, "w")
        fcntl.flock(_lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_file.write(str(os.getpid()))
        _lock_file.flush()
        return True
    except (IOError, OSError):
        logger.error(
            "Another bot instance is already running. Exiting to avoid duplicates."
        )
        return False


def register_shutdown(bot: NightCityBot):
    """Register signal handlers for a graceful shutdown."""

    async def shutdown():
        logger.info("Shutdown signal received, cleaning up...")
        await bot.close()
        logger.info("Shutdown complete")

    def handler(signum, frame):
        bot.loop.create_task(shutdown())

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, handler)


def main():
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
    )
    logger.info("Starting NightCityBot...")

    if not acquire_instance_lock():
        logger.error("Another bot instance is already running. Exiting.")
        sys.exit(1)
    logger.info("Instance lock acquired.")

    try:
        if keep_alive():
            logger.info("Keep-alive server started.")
        else:
            logger.info("Keep-alive server disabled.")
    except Exception:
        logger.exception("Failed to start keep-alive server")

    if not config.TOKEN:
        logger.error("No Discord token found! Please set TOKEN in Secrets.")
        return

    logger.info("Token found, connecting to Discord...")

    try:
        bot = NightCityBot()
        register_shutdown(bot)
    except Exception:
        logger.exception("Failed to create bot instance")
        return

    try:
        bot.run(config.TOKEN)
    except discord.LoginFailure:
        logger.error("Invalid Discord token!")
    except Exception:
        logger.exception("Bot startup failed")


if __name__ == "__main__":
    main()
