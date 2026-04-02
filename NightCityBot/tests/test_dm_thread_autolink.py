from typing import List
import discord
from unittest.mock import AsyncMock, MagicMock, patch
import config

async def run(suite, ctx) -> List[str]:
    """Auto-link DM threads when mapping is missing."""
    logs: List[str] = []
    dm_handler = suite.bot.get_cog('DMHandler')
    real_user = await suite.get_test_user(ctx)

    mock_user = MagicMock(spec=discord.Member)
    mock_user.id = real_user.id
    mock_user.name = real_user.name
    mock_user.display_name = real_user.display_name
    mock_user.send = AsyncMock()

    dm_handler.dm_threads = {}
    thread = MagicMock(spec=discord.Thread)
    thread.id = 4242
    thread.name = f"{mock_user.name}-{mock_user.id}"
    thread.parent_id = config.DM_INBOX_CHANNEL_ID
    thread.send = AsyncMock()
    message = MagicMock()
    message.channel = thread
    message.content = "Hello"
    message.attachments = []
    fixer_role = MagicMock()
    fixer_role.id = config.FIXER_ROLE_ID
    message.author = MagicMock(roles=[fixer_role], display_name="Fixer", id=1)
    message.delete = AsyncMock()
    with patch.object(dm_handler.bot, 'fetch_user', new=AsyncMock(return_value=mock_user)):
        await dm_handler.handle_thread_message(message)
    suite.assert_called(logs, mock_user.send, 'user.send')
    if str(mock_user.id) in dm_handler.dm_threads and dm_handler.dm_threads[str(mock_user.id)] == thread.id:
        logs.append('✅ Thread auto-linked from name')
    else:
        logs.append('❌ Thread mapping not updated')
    return logs
