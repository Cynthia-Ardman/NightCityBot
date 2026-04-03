from typing import List

async def run(suite, ctx) -> List[str]:
    """Smoke-check that CyberwareShop cog is loaded (commands migrated to hub)."""
    logs: List[str] = []
    cog = suite.bot.get_cog("CyberwareShop")
    if not cog:
        logs.append("❌ CyberwareShop is not loaded")
    else:
        logs.append("✅ CyberwareShop cog is loaded")
    return logs
