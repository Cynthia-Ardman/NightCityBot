import discord
from discord.ext import commands
import config
from NightCityBot.utils.db import system_settings_get_all, system_settings_set

SYSTEMS = [
    "cyberware",
    "cyberware_shop",
    "gun_shop",
    "player_inventory",
    "attend",
    "open_shop",
    "loa",
    "housing_rent",
    "business_rent",
    "trauma_team",
    "dm",
    "auto_collect_rent",
]

# Systems that should be OFF by default; everything else defaults to ON.
_OFF_BY_DEFAULT = {"auto_collect_rent", "player_inventory"}


class SystemControl(commands.Cog):
    """Enable or disable major bot systems."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.status = {}
        self.bot.loop.create_task(self.load_status())

    async def load_status(self):
        raw = await system_settings_get_all()
        # Only keep known systems; discard stale DB keys (e.g. old "wholesaler").
        self.status = {k: v for k, v in raw.items() if k in SYSTEMS}
        updated = False
        for system in SYSTEMS:
            if system not in self.status:
                self.status[system] = system not in _OFF_BY_DEFAULT
                updated = True
        if updated:
            for name, val in self.status.items():
                await system_settings_set(name, val)

    def is_enabled(self, system: str) -> bool:
        return self.status.get(system, False)

    async def set_status(self, system: str, value: bool):
        if system not in SYSTEMS:
            return False
        self.status[system] = value
        await system_settings_set(system, value)
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
        lines = [
            f"{name}: {'ON' if self.status.get(name, False) else 'OFF'}"
            for name in SYSTEMS
        ]
        await ctx.send("\n".join(lines))
