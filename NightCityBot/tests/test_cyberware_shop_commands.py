from typing import List

async def run(suite, ctx) -> List[str]:
    """Smoke-check that CyberwareShop is loaded and key commands are registered."""
    logs: List[str] = []
    cog = suite.bot.get_cog("CyberwareShop")
    if not cog:
        logs.append("❌ CyberwareShop is not loaded")
        return logs

    required = {
        "cw_catalog",
        "cw_add",
        "cw_remove",
        "cw_give",
        "cw_take",
        "cw_buy",
        "cw_inventory",
        "cw_sell",
        "cw_tx",
        "cw_wh_list",
        "cw_wh_restock",
        "cw_wh_add",
        "cw_wh_remove",
        "cw_wh_settings",
        "cw_setsheet",
    }
    available = {cmd.name for cmd in cog.get_commands()}

    missing = sorted(required - available)
    if missing:
        logs.append(f"❌ Missing cyberware shop commands: {', '.join(missing)}")
    else:
        logs.append("✅ Cyberware shop commands are registered")
    return logs
