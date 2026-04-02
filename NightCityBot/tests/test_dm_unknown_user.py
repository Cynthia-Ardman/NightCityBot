from typing import List
import discord
from unittest.mock import AsyncMock, MagicMock, patch
import config

async def run(suite, ctx) -> List[str]:
    """Handle missing user IDs gracefully."""
    logs: List[str] = []
    dm_handler = suite.bot.get_cog('DMHandler')
    user = await suite.get_test_user(ctx)
    thread = MagicMock(spec=discord.Thread)
    thread.id = 9999
    thread.name = f"{user.name}-{user.id}"
    thread.parent_id = config.DM_INBOX_CHANNEL_ID
    message = MagicMock()
    message.channel = thread
    message.content = "Hello"
    message.attachments = []
    fixer_role = MagicMock()
    fixer_role.id = config.FIXER_ROLE_ID
    message.author = MagicMock(roles=[fixer_role], display_name="Fixer", id=1)
    message.delete = AsyncMock()

    dm_handler.dm_threads[str(user.id)] = thread.id

    notfound = discord.NotFound(MagicMock(), {"message": "Unknown"})
    with patch.object(dm_handler.bot, 'fetch_user', new=AsyncMock(side_effect=notfound)):
        await dm_handler.handle_thread_message(message)

    if str(user.id) not in dm_handler.dm_threads:
        logs.append("✅ Unknown user handled gracefully")
    else:
        logs.append("❌ Unknown user mapping not removed")
    return logs
