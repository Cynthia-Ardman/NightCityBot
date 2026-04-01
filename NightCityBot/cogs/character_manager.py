import difflib
import logging
import re
import time

try:
    from rapidfuzz.fuzz import partial_ratio as fuzz_ratio
except Exception:  # pragma: no cover - rapidfuzz may not be installed
    def fuzz_ratio(a: str, b: str) -> float:
        return difflib.SequenceMatcher(None, a, b).ratio() * 100

import discord
from discord.ext import commands
from NightCityBot.utils.permissions import is_fixer, is_cs_approver
from NightCityBot.utils.helpers import save_json_file, safe_filename
from pathlib import Path
import config


logger = logging.getLogger(__name__)


class CharacterManager(commands.Cog):
    """Manage character sheet threads."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.sheet_index: dict[int, dict] = {}
        self.index_time: float = 0.0

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _move_thread(
        self, thread: discord.Thread, destination: discord.ForumChannel
    ) -> bool:
        """Move ``thread`` to ``destination`` forum channel.

        Returns ``True`` if the thread was moved successfully.
        """
        try:
            await thread._state.http.edit_channel(
                thread.id, parent_id=str(destination.id)
            )
        except Exception as e:  # discord.Forbidden, discord.NotFound, HTTPException
            logger.warning(
                "Failed to move thread %s (%s) to %s: %s",
                thread.name,
                thread.id,
                destination.name,
                e,
            )
            return False

        logger.info(
            "Moved thread %s (%s) to %s",
            thread.name,
            thread.id,
            destination.name,
        )
        return True

    async def _copy_thread(
        self, thread: discord.Thread, destination: discord.ForumChannel
    ) -> bool:
        """Copy ``thread`` messages to a new thread in ``destination`` and delete the original."""
        try:
            messages = [
                msg
                async for msg in thread.history(limit=None, oldest_first=True)
            ]

            files = []
            content = None
            if messages:
                first = messages.pop(0)
                full_content = first.content or ""
                files = [await a.to_file() for a in first.attachments]
                content = full_content[:2000] if full_content else None
                remainder = full_content[2000:] if len(full_content) > 2000 else None
            else:
                remainder = None

            kwargs = {"name": thread.name}
            if content is not None:
                kwargs["content"] = content
            if files:
                kwargs["files"] = files
            created = await destination.create_thread(**kwargs)
            new_thread = created.thread if hasattr(created, "thread") else created

            if remainder:
                for i in range(0, len(remainder), 2000):
                    await new_thread.send(content=remainder[i : i + 2000])

            for msg in messages:
                msg_files = [await a.to_file() for a in msg.attachments]
                if msg.content or msg_files:
                    await new_thread.send(content=msg.content or None, files=msg_files or None)

            await thread.delete()
        except Exception as e:  # pragma: no cover - network/permission errors
            logger.warning(
                "Failed to copy thread %s (%s) to %s: %s",
                thread.name,
                thread.id,
                destination.name,
                e,
            )
            return False

        logger.info(
            "Archived thread %s (%s) to %s",
            thread.name,
            thread.id,
            destination.name,
        )
        return True

    async def _iter_all_threads(self, forum: discord.ForumChannel):
        """Yield all threads from ``forum`` including archived ones."""
        for t in forum.threads:
            yield t
        async for t in forum.archived_threads(limit=None):
            yield t

    async def _ensure_index(self, forums: list[discord.ForumChannel]) -> None:
        """Build or refresh the sheet index."""
        now = time.monotonic()
        if self.sheet_index and now - self.index_time < 3600:
            return

        self.sheet_index.clear()
        for forum in forums:
            async for thread in self._iter_all_threads(forum):
                first = ""
                async for msg in thread.history(limit=1, oldest_first=True):
                    first = msg.content or ""
                    break
                self.sheet_index[thread.id] = {
                    "thread": thread,
                    "title": thread.name,
                    "tags": [t.name for t in thread.applied_tags],
                    "first": first,
                }

        self.index_time = now

    def _match(self, query: str, text: str) -> bool:
        q = query.lower()
        t = text.lower()
        if q in t:
            return True
        return fuzz_ratio(q, t) > 60

    def _highlight(self, text: str, keyword: str) -> str:
        pattern = re.compile(re.escape(keyword), re.I)
        return pattern.sub(lambda m: f"**{m.group(0)}**", text)

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    @commands.command()
    @is_fixer()
    async def retire(self, ctx: commands.Context) -> None:
        """Move all threads tagged "Retired" to the retired forum."""
        src = ctx.guild.get_channel(config.CHARACTER_SHEETS_CHANNEL_ID)
        dest = ctx.guild.get_channel(config.RETIRED_SHEETS_CHANNEL_ID)
        if not isinstance(src, discord.ForumChannel) or not isinstance(
            dest, discord.ForumChannel
        ):
            await ctx.send("⚠️ Character forums not configured.")
            return
        retired_tag = discord.utils.get(src.available_tags, name="Retired")
        if not retired_tag:
            await ctx.send("⚠️ 'Retired' tag not found.")
            return
        moved = 0
        failed = 0
        async for thread in self._iter_all_threads(src):
            if any(tag.id == retired_tag.id for tag in thread.applied_tags):
                if await self._copy_thread(thread, dest):
                    moved += 1
                else:
                    failed += 1
        message = f"✅ Moved {moved} thread(s) to {dest.name}."
        if failed:
            message += f" ❌ Failed to move {failed} thread(s). Check logs."
        await ctx.send(message)

    @commands.command()
    @is_fixer()
    async def move_npcs(self, ctx: commands.Context) -> None:
        """Move all threads tagged "NPC" to the NPC forum."""
        src = ctx.guild.get_channel(config.CHARACTER_SHEETS_CHANNEL_ID)
        dest = ctx.guild.get_channel(config.NPC_SHEETS_CHANNEL_ID)
        if not isinstance(src, discord.ForumChannel) or not isinstance(
            dest, discord.ForumChannel
        ):
            await ctx.send("⚠️ Character forums not configured.")
            return
        npc_tag = discord.utils.get(src.available_tags, name="NPC")
        if not npc_tag:
            await ctx.send("⚠️ 'NPC' tag not found.")
            return
        moved = 0
        failed = 0
        async for thread in self._iter_all_threads(src):
            if any(tag.id == npc_tag.id for tag in thread.applied_tags):
                if await self._copy_thread(thread, dest):
                    moved += 1
                else:
                    failed += 1
        message = f"✅ Moved {moved} thread(s) to {dest.name}."
        if failed:
            message += f" ❌ Failed to move {failed} thread(s). Check logs."
        await ctx.send(message)

    @commands.command()
    @is_fixer()
    async def unretire(self, ctx: commands.Context, thread_id: int) -> None:
        """Move a single thread back to the main forum."""
        src = ctx.guild.get_channel(config.CHARACTER_SHEETS_CHANNEL_ID)
        dest = ctx.guild.get_channel(config.RETIRED_SHEETS_CHANNEL_ID)
        if not isinstance(src, discord.ForumChannel) or not isinstance(
            dest, discord.ForumChannel
        ):
            await ctx.send("⚠️ Character forums not configured.")
            return
        try:
            thread = await self.bot.fetch_channel(thread_id)
        except discord.NotFound:
            await ctx.send("❌ Thread not found.")
            return
        if not isinstance(thread, discord.Thread) or thread.parent_id != dest.id:
            await ctx.send("❌ Invalid thread ID.")
            return
        await self._move_thread(thread, src)
        await ctx.send(f"✅ {thread.name} moved back to {src.name}.")

    @commands.command(aliases=["sheet_search", "search_sheets"])
    @commands.check_any(is_fixer(), is_cs_approver())
    async def search_characters(self, ctx: commands.Context, *, keyword: str) -> None:
        """Search character sheets for ``keyword``.

        Use ``-depth N`` to scan up to ``N`` messages per thread (default 20)."""
        forums: list[discord.ForumChannel] = []
        for cid in (
            config.CHARACTER_SHEETS_CHANNEL_ID,
            config.RETIRED_SHEETS_CHANNEL_ID,
        ):
            ch = ctx.guild.get_channel(cid)
            if isinstance(ch, discord.ForumChannel):
                forums.append(ch)
        if not forums:
            await ctx.send("⚠️ Character forums not configured.")
            return

        parts = keyword.split()
        depth = 20
        kw_parts = []
        i = 0
        while i < len(parts):
            part = parts[i]
            if part.lower() in {"-depth", "--depth"} and i + 1 < len(parts):
                try:
                    depth = int(parts[i + 1])
                except ValueError:
                    pass
                i += 2
                continue
            kw_parts.append(part)
            i += 1
        keyword = " ".join(kw_parts)

        await self._ensure_index(forums)

        matches: list[tuple[discord.Thread, str, str]] = []
        for data in self.sheet_index.values():
            thread: discord.Thread = data["thread"]
            if (
                self._match(keyword, data["title"])
                or any(self._match(keyword, t) for t in data["tags"])
                or self._match(keyword, data["first"])
            ):
                text = data["first"] or data["title"]
                loc = text.lower().find(keyword.lower())
                snippet = text[max(0, loc - 30) : loc + len(keyword) + 30] if loc != -1 else text[:60]
                matches.append((thread, snippet, thread.jump_url))
                if len(matches) >= 10:
                    break

        if len(matches) < 10 and depth:
            for data in self.sheet_index.values():
                if any(mth.id == data["thread"].id for mth, _, _ in matches):
                    continue
                thread: discord.Thread = data["thread"]
                async for msg in thread.history(limit=depth, oldest_first=True):
                    if msg.content and self._match(keyword, msg.content):
                        text = msg.content
                        loc = text.lower().find(keyword.lower())
                        snippet = text[max(0, loc - 30) : loc + len(keyword) + 30] if loc != -1 else text[:60]
                        matches.append((thread, snippet, msg.jump_url))
                        break
                if len(matches) >= 10:
                    break

        if not matches:
            await ctx.send("No results found.")
            return

        embed = discord.Embed(title=f"Results for '{keyword}'", color=discord.Color.blurple())
        for th, snippet, url in matches:
            embed.add_field(name=th.name, value=f"{self._highlight(snippet, keyword)}\n{url}", inline=False)
        await ctx.send(embed=embed)

    @commands.command(name="backup_sheets")
    @is_fixer()
    async def backup_sheets(self, ctx: commands.Context) -> None:
        """Back up all character sheet threads to files."""
        forums: list[discord.ForumChannel] = []
        for cid in (
            config.CHARACTER_SHEETS_CHANNEL_ID,
            config.RETIRED_SHEETS_CHANNEL_ID,
        ):
            ch = ctx.guild.get_channel(cid)
            if isinstance(ch, discord.ForumChannel):
                forums.append(ch)
        if not forums:
            await ctx.send("⚠️ Character forums not configured.")
            return

        backup_dir = Path(config.CHARACTER_BACKUP_DIR)
        # Ensure the entire path exists in case CHARACTER_BACKUP_DIR contains
        # intermediate directories that haven't been created yet.
        backup_dir.mkdir(parents=True, exist_ok=True)
        logger.info("Backing up character sheets to %s", backup_dir)

        saved = 0
        for forum in forums:
            logger.debug("Processing forum: %s (%s)", forum.name, forum.id)
            async for thread in self._iter_all_threads(forum):
                data = {
                    "id": thread.id,
                    "title": thread.name,
                    "tags": [t.name for t in thread.applied_tags],
                    "messages": [],
                }
                async for msg in thread.history(limit=None, oldest_first=True):
                    data["messages"].append(
                        {
                            "author_id": msg.author.id,
                            "author": getattr(msg.author, "display_name", ""),
                            "content": msg.content or "",
                            "created_at": msg.created_at.isoformat(),
                            "attachments": [a.url for a in msg.attachments],
                        }
                    )

                filename = f"{safe_filename(thread.name)}_{thread.id}.json"
                path = backup_dir / filename
                logger.debug(
                    "Saving thread %s (%s) with %s message(s) to %s",
                    thread.name,
                    thread.id,
                    len(data["messages"]),
                    path,
                )
                if await save_json_file(path, data):
                    saved += 1

        logger.info("Backed up %s sheet(s) to %s", saved, backup_dir)
        await ctx.send(f"✅ Backed up {saved} sheet(s) to `{backup_dir.name}`.")
