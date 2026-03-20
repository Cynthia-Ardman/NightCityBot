import discord
from discord.ext import commands
import config
from NightCityBot.utils.db import db_load, db_save

SYSTEMS = [
    "cyberware",
    "attend",
    "open_shop",
    "wholesaler",
    "loa",
    "housing_rent",
    "business_rent",
    "trauma_team",
    "dm",
]

class SystemControl(commands.Cog):
    """Enable or disable major bot systems."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.status = {}
        self.bot.loop.create_task(self.load_status())

    async def load_status(self):
        self.status = await db_load(
            "system_status", default={}, seed_path=config.SYSTEM_STATUS_FILE
        )
        updated = False
        for system in SYSTEMS:
            if system not in self.status:
                self.status[system] = system == "wholesaler"
                updated = True
        if updated:
            await db_save("system_status", self.status)

    def is_enabled(self, system: str) -> bool:
        return self.status.get(system, False)

    async def set_status(self, system: str, value: bool):
        if system not in SYSTEMS:
            return False
        self.status[system] = value
        await db_save("system_status", self.status)
        return True

    @commands.command(aliases=["enablesystem", "es", "systemenable"])
    @commands.has_permissions(administrator=True)
    async def enable_system(self, ctx, system: str):
        """Enable a disabled system."""
        system = system.lower()
        if system == "all":
            for name in SYSTEMS:
                await self.set_status(name, True)
            await ctx.send("✅ Enabled all systems.")
            return
        if not await self.set_status(system, True):
            await ctx.send(f"❌ Unknown system '{system}'.")
            return
        await ctx.send(f"✅ Enabled {system} system.")

    @commands.command(aliases=["disablesystem", "ds", "systemdisable"])
    @commands.has_permissions(administrator=True)
    async def disable_system(self, ctx, system: str):
        """Disable an active system."""
        system = system.lower()
        if system == "all":
            for name in SYSTEMS:
                await self.set_status(name, False)
            await ctx.send("✅ Disabled all systems.")
            return
        if not await self.set_status(system, False):
            await ctx.send(f"❌ Unknown system '{system}'.")
            return
        await ctx.send(f"✅ Disabled {system} system.")

    @commands.command(name="system_status", aliases=["systemstatus"])
    @commands.has_permissions(administrator=True)
    async def system_status(self, ctx):
        """Show current system enablement."""
        lines = [f"{name}: {'ON' if state else 'OFF'}" for name, state in self.status.items()]
        await ctx.send("\n".join(lines))
