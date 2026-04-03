from typing import List

async def run(suite, ctx) -> List[str]:
    """Smoke-check that GunsShopCog is loaded (commands migrated to hub)."""
    logs: List[str] = []
    cog = suite.bot.get_cog("GunsShopCog")
    if not cog:
        logs.append("❌ GunsShopCog is not loaded")
    else:
        logs.append("✅ GunsShopCog cog is loaded")
    return logs
