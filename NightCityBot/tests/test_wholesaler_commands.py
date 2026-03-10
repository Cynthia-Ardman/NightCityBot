from typing import List

async def run(suite, ctx) -> List[str]:
    """Smoke-check that WholesalerCog is loaded and key commands are registered."""
    logs: List[str] = []
    cog = suite.bot.get_cog("WholesalerCog")
    if not cog:
        logs.append("❌ WholesalerCog is not loaded")
        return logs

    required = {
        "wh_list",
        "wh_buy",
        "store_inv",
        "wh_sell",
        "wh_restock",
        "wh_clear_inventory",
        "wh_setshop",
        "wh_shops",
        "wh_setsheet",
        "wh_recheck",
        "wh_paths",
    }
    available = {cmd.name for cmd in cog.get_commands()}

    missing = sorted(required - available)
    if missing:
        logs.append(f"❌ Missing wholesaler commands: {', '.join(missing)}")
    else:
        logs.append("✅ Wholesaler commands are registered")
    return logs
