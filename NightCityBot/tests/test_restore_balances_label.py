from typing import List
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path
import config

async def run(suite, ctx) -> List[str]:
    """Restore all balances using a label."""
    logs: List[str] = []
    economy = suite.bot.get_cog('Economy')
    member = await suite.get_test_user(ctx)
    ctx.send = AsyncMock()

    backup_dir = Path(config.BALANCE_BACKUP_DIR)
    path1 = backup_dir / f"balance_backup_{member.id}.json"
    path2 = backup_dir / "balance_backup_123.json"

    backups = {
        str(path1): [{"cash": 100, "bank": 50, "label": "collect_rent_before"}],
        str(path2): [{"cash": 200, "bank": 0, "label": "collect_rent_before"}],
    }

    async def fake_load(p, default=None):
        return backups.get(str(p), default if default is not None else [])

    with (
        patch("pathlib.Path.glob", return_value=[path1, path2]),
        patch("NightCityBot.cogs.economy.load_json_file", new=AsyncMock(side_effect=fake_load)),
        patch.object(economy.unbelievaboat, "get_balance", new=AsyncMock(return_value={"cash": 0, "bank": 0})),
        patch.object(economy.unbelievaboat, "update_balance", new=AsyncMock(return_value=True)) as mock_update,
    ):
        await economy.restore_balances_command(ctx, "collect_rent_before")
        suite.assert_called(logs, mock_update, "update_balance")
        if mock_update.await_count >= 2:
            logs.append("✅ multiple restores")
        else:
            logs.append(f"❌ expected 2 updates got {mock_update.await_count}")

    return logs
