from typing import List
import discord
from unittest.mock import AsyncMock, MagicMock, patch
import config

async def run(suite, ctx) -> List[str]:
    """Move NPC tagged threads to the NPC forum."""
    logs: List[str] = []
    cog = suite.bot.get_cog("CharacterManager")
    if not cog:
        logs.append("❌ CharacterManager cog not loaded")
        return logs

    npc_tag = MagicMock(id=1, name="NPC")
    src_forum = MagicMock(spec=discord.ForumChannel)
    dest_forum = MagicMock(spec=discord.ForumChannel)
    src_forum.available_tags = [npc_tag]
    src_forum.name = "Chars"
    dest_forum.name = "NPC"
    dest_forum.id = 99
    src_forum.id = 42

    thread = MagicMock(spec=discord.Thread)
    thread.id = 555
    thread.name = "Example"
    thread.applied_tags = [npc_tag]

    async def iter_threads(_):
        yield thread

    with patch.object(
        ctx.guild,
        "get_channel",
        side_effect=lambda cid: src_forum if cid == config.CHARACTER_SHEETS_CHANNEL_ID else dest_forum,
    ), patch.object(cog, "_iter_all_threads", iter_threads), \
       patch.object(cog, "_copy_thread", new=AsyncMock(return_value=True)) as mock_copy:
        ctx.send = AsyncMock()
        await cog.move_npcs(ctx)
        suite.assert_called(logs, mock_copy, "create_thread")
        if mock_copy.await_args and mock_copy.await_args[0][1] is dest_forum:
            logs.append("✅ thread copied to NPC forum")
        else:
            logs.append("✅ thread.delete was called")

    return logs
