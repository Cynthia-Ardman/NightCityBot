from typing import List

async def run(suite, ctx) -> List[str]:
    """Smoke-check that GunsShopCog is loaded and key commands are registered."""
    logs: List[str] = []
    cog = suite.bot.get_cog("GunsShopCog")
    if not cog:
        logs.append("❌ GunsShopCog is not loaded")
        return logs

    required = {
        "guns_wh_list",
        "guns_wh_buy",
        "guns_store_inv",
        "guns_wh_sell",
        "guns_wh_restock",
        "guns_wh_clear_inventory",
        "guns_wh_setshop",
        "guns_wh_shops",
        "guns_wh_setsheet",
        "guns_wh_recheck",
        "guns_wh_paths",
    }
    available = {cmd.name for cmd in cog.get_commands()}

    missing = sorted(required - available)
    if missing:
        logs.append(f"❌ Missing gun shop commands: {', '.join(missing)}")
    else:
        logs.append("✅ Gun shop commands are registered")
    return logs
