import asyncio
import logging
import re

import discord
from discord.ext import commands
from discord.abc import Messageable

import config
from NightCityBot.utils.permissions import is_fixer
from NightCityBot.utils.db import dm_thread_get_all, dm_thread_set, dm_thread_delete

logger = logging.getLogger(__name__)


def _relay_description(message: discord.Message) -> str:
    """Return a short description for audit logs when deleting a relay."""
    if message.content.strip():
        return message.content
    if message.attachments:
        if len(message.attachments) == 1:
            return message.attachments[0].filename
        return "attachment"
    return ""


class DMHandler(commands.Cog):
    """Cog handling anonymous DM relays and logging threads."""

    def __init__(self, bot: commands.Bot) -> None:
        """Initialize the DM handler."""
        self.bot = bot
        self.dm_threads: dict[str, int] = {}
        self.load_event = asyncio.Event()
        self.thread_lock = asyncio.Lock()
        self.bot.loop.create_task(self.load_thread_cache())

    async def load_thread_cache(self) -> None:
        """Load the thread mapping cache on startup."""
        self.dm_threads = await dm_thread_get_all()
        self.load_event.set()

    async def get_or_create_dm_thread(
            self,
            user: discord.abc.User
    ) -> discord.Thread | discord.TextChannel:
        """Return the logging thread for a DM sender, creating it if necessary."""
        await self.load_event.wait()
        async with self.thread_lock:
            log_channel = self.bot.get_channel(config.DM_INBOX_CHANNEL_ID)
            user_id = str(user.id)

            if user_id in self.dm_threads:
                try:
                    thread = await self.bot.fetch_channel(self.dm_threads[user_id])
                    if isinstance(thread, discord.Thread) and thread.archived:
                        await thread.edit(archived=False)
                    return thread
                except discord.NotFound:
                    pass  # Thread was deleted, create new one

            # Look for an existing thread if it's not in the cache
            expected_name = f"{user.name}-{user.id}".replace(" ", "-").lower()[:100]
            if log_channel is None:
                raise RuntimeError(
                    f"DM inbox channel {getattr(config, 'DM_INBOX_CHANNEL_ID', '?')} "
                    "not found — check DM_INBOX_CHANNEL_ID in config."
                )
            if isinstance(log_channel, (discord.TextChannel, discord.ForumChannel)):
                for t in log_channel.threads:
                    if t.name == expected_name:
                        if t.archived:
                            await t.edit(archived=False)
                        self.dm_threads[user_id] = t.id
                        await dm_thread_set(user_id, t.id)
                        return t

            thread_name = f"{user.name}-{user.id}".replace(" ", "-").lower()[:100]

            if isinstance(log_channel, discord.TextChannel):
                thread = await log_channel.create_thread(
                    name=thread_name,
                    type=discord.ChannelType.private_thread,
                    reason=f"Logging DM history for {user}"
                )
            elif isinstance(log_channel, discord.ForumChannel):
                created = await log_channel.create_thread(
                    name=thread_name,
                    content=f"📥 DM started with {user}.",
                    reason=f"Logging DM history for {user}"
                )
                thread = created.thread if hasattr(created, "thread") else created
            else:
                raise RuntimeError("DM inbox must be a TextChannel or ForumChannel")

            self.dm_threads[user_id] = thread.id
            await dm_thread_set(user_id, thread.id)

            return thread

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author == self.bot.user or message.author.bot:
            return

        control = self.bot.get_cog('SystemControl')
        if control and not control.is_enabled('dm'):
            return

        # Handle relay from Fixer DM-forum threads
        if isinstance(message.channel, discord.Thread):
            await self.handle_thread_message(message)

        # Handle incoming user DMs
        elif isinstance(message.channel, discord.DMChannel):
            await self.handle_dm_message(message)

    async def handle_thread_message(self, message: discord.Message):
        """Handle messages sent in DM logging threads."""
        await self.load_event.wait()
        parent_id = getattr(message.channel, "parent_id", None)
        if parent_id != config.DM_INBOX_CHANNEL_ID:
            return
        user_id: str | None = None
        for uid, thread_id in self.dm_threads.items():
            if message.channel.id == thread_id:
                user_id = uid
                break

        if user_id is None:
            match = re.search(r"(\d+)$", message.channel.name)
            if match:
                user_id = match.group(1)
                self.dm_threads[user_id] = message.channel.id
                await dm_thread_set(user_id, message.channel.id)

        if user_id is None:
            return

        roles = getattr(message.author, "roles", [])
        if not any(getattr(r, "id", 0) == config.FIXER_ROLE_ID for r in roles):
            return

        try:
            target_user = await self.bot.fetch_user(int(user_id))
        except discord.NotFound:
            logger.warning("DM relay failed: unknown user %s", user_id)
            self.dm_threads.pop(user_id, None)
            await dm_thread_delete(user_id)
            return
        if not target_user:
            return

        # Handle roll command relay
        if message.content.strip().lower().startswith("!roll"):
            roll_cog = self.bot.get_cog('RollSystem')
            if roll_cog:
                dice = message.content.strip()[len("!roll"):].strip()
                ctx = await self.bot.get_context(message)
                setattr(ctx, "original_author", message.author)
                ctx.author = target_user
                ctx.channel = await target_user.create_dm()
                await roll_cog.roll(ctx, dice=dice)
            try:
                await message.delete()
                admin = self.bot.get_cog('Admin')
                if admin:
                    await admin.log_audit(message.author, f"🗑️ Deleted DM relay: {_relay_description(message)}")
            except Exception:
                logger.warning("Suppressed exception", exc_info=True)
            return

        # Handle start-rp command relay
        if message.content.strip().lower().startswith("!start-rp"):
            rp_cog = self.bot.get_cog('RPManager')
            if rp_cog:
                args_str = message.content.strip()[len("!start-rp"):].strip()
                if args_str:
                    args = args_str.split()
                else:
                    args = [f"<@{target_user.id}>"]
                ctx = await self.bot.get_context(message)
                await rp_cog.start_rp(ctx, *args)
            try:
                await message.delete()
                admin = self.bot.get_cog('Admin')
                if admin:
                    await admin.log_audit(message.author, f"🗑️ Deleted DM relay: {_relay_description(message)}")
            except Exception:
                logger.warning("Suppressed exception", exc_info=True)
            return

        if message.content.strip().startswith("!"):
            ctx = await self.bot.get_context(message)
            admin = self.bot.get_cog('Admin')
            async def audit_send(content=None, **kwargs):
                if admin and content:
                    await admin.log_audit(message.author, content)
            ctx.send = audit_send
            await self.bot.invoke(ctx)
            try:
                await message.delete()
                admin = self.bot.get_cog('Admin')
                if admin:
                    await admin.log_audit(message.author, f"🗑️ Deleted DM relay: {_relay_description(message)}")
            except Exception:
                logger.warning("Suppressed exception", exc_info=True)
            return

        # Handle normal message relay
        user_files = []
        log_files = []
        for a in message.attachments:
            if a.size > 8 * 1024 * 1024:
                await message.channel.send(
                    f"⚠️ Attachment '{a.filename}' too large to forward."
                )
            else:
                user_files.append(await a.to_file())
                log_files.append(await a.to_file())
        outgoing = message.content or ""
        try:
            if len(outgoing) <= 2000:
                await target_user.send(content=outgoing or None, files=user_files)
            else:
                for i in range(0, len(outgoing), 2000):
                    chunk = outgoing[i:i + 2000]
                    f = user_files if i == 0 else []
                    await target_user.send(content=chunk, files=f)
        except discord.HTTPException:
            await message.channel.send("⚠️ Failed to forward message — attachment too large.")
        log_header = (
            f"📤 **Sent to {target_user.display_name} ({target_user.id}) "
            f"by {message.author.display_name} ({message.author.id}):**\n"
        )
        log_full = f"{log_header}{outgoing}"
        if len(log_full) <= 2000:
            await message.channel.send(log_full, files=log_files)
        else:
            await message.channel.send(log_full[:2000], files=log_files)
            for i in range(2000, len(log_full), 2000):
                await message.channel.send(log_full[i:i + 2000])
        try:
            await message.delete()
            admin = self.bot.get_cog('Admin')
            if admin:
                await admin.log_audit(message.author, f"🗑️ Deleted DM relay: {_relay_description(message)}")
        except Exception:
            logger.warning("Suppressed exception", exc_info=True)

    async def handle_dm_message(self, message: discord.Message):
        """Handle incoming DMs from users."""
        control = self.bot.get_cog('SystemControl')
        if control and not control.is_enabled('dm'):
            return
        try:
            thread = await self.get_or_create_dm_thread(message.author)
            msg_target: Messageable = thread

            full = message.content or "*(No text content)*"
            for chunk in (full[i:i + 1024] for i in range(0, len(full), 1024)):
                if chunk.strip().startswith("!"):
                    continue
                await msg_target.send(
                    f"📥 **Received from {message.author.display_name} ({message.author.id})**:\n{chunk}"
                )

            for att in message.attachments:
                await msg_target.send(f"📎 Received attachment: {att.url}")
        except Exception as e:
            logger.exception("DM logging failed: %s", e)

    @commands.command()
    @is_fixer()
    async def dm(self, ctx, user: discord.User, *, message=None):
        """Send an anonymous DM to a user."""
        control = self.bot.get_cog('SystemControl')
        if control and not control.is_enabled('dm'):
            await ctx.send("⚠️ The dm system is currently disabled.")
            try:
                await ctx.message.delete()
                admin = self.bot.get_cog('Admin')
                if admin:
                    await admin.log_audit(ctx.author, f"🗑️ Deleted command: {ctx.message.content}")
            except Exception:
                logger.warning("Suppressed exception", exc_info=True)
            return
        file_links = [attachment.url for attachment in ctx.message.attachments]

        # Handle roll command relay
        if message and message.strip().lower().startswith("!roll"):
            roll_cog = self.bot.get_cog('RollSystem')
            if roll_cog:
                dice = message.strip()[len("!roll"):].strip()
                pattern = r"(?:(\d*)d)?(\d+)([+-]\d+)?"
                if not re.fullmatch(pattern, dice.replace(" ", "")):
                    await ctx.send(
                        "🎲 Format: `!roll XdY+Z` (e.g. `!roll 2d6+3`)")
                    try:
                        await ctx.message.delete()
                        admin = self.bot.get_cog('Admin')
                        if admin:
                            await admin.log_audit(
                                ctx.author,
                                f"🗑️ Deleted command: {ctx.message.content}")
                    except Exception:
                        logger.warning("Suppressed exception", exc_info=True)
                    return
                member = (ctx.guild.get_member(user.id) if ctx.guild else None) or user
                fake_ctx = await self.bot.get_context(ctx.message)
                fake_ctx.author = member
                fake_ctx.channel = await user.create_dm()
                setattr(fake_ctx, "original_author", ctx.author)
                thread = await self.get_or_create_dm_thread(user)
                await roll_cog.roll(fake_ctx, dice=dice)
                admin = self.bot.get_cog('Admin')
                if admin:
                    await admin.log_audit(
                        ctx.author,
                        f"✅ Rolled `{dice}` anonymously for {user.display_name}.",
                    )

            try:
                await ctx.message.delete()
                admin = self.bot.get_cog('Admin')
                if admin:
                    await admin.log_audit(ctx.author, f"🗑️ Deleted command: {ctx.message.content}")
            except Exception:
                logger.warning("Suppressed exception", exc_info=True)
            return

        # Handle normal DM
        dm_content_parts = [message] if message else []
        if file_links:
            links_formatted = "\n".join(file_links)
            dm_content_parts.append(f"📎 **Attachments:**\n{links_formatted}")
        dm_content = "\n\n".join(dm_content_parts) if dm_content_parts else "(No text)"

        try:
            if len(dm_content) <= 2000:
                await user.send(content=dm_content)
            else:
                for i in range(0, len(dm_content), 2000):
                    await user.send(content=dm_content[i:i + 2000])

            thread = await self.get_or_create_dm_thread(user)
            if isinstance(thread, (discord.Thread, discord.TextChannel)):
                log_msg = f"📤 **Sent to {user.display_name} ({user.id}) by {ctx.author.display_name} ({ctx.author.id}):**\n{dm_content}"
                if len(log_msg) <= 2000:
                    await thread.send(log_msg)
                else:
                    await thread.send(log_msg[:2000])
                    for i in range(2000, len(log_msg), 2000):
                        await thread.send(log_msg[i:i + 2000])
            else:
                logger.error("Cannot log DM — thread type is %s", type(thread))

            admin = self.bot.get_cog('Admin')
            if admin:
                await admin.log_audit(ctx.author, f"✅ DM sent anonymously to {user.display_name}.")

        except discord.Forbidden:
            await ctx.send('❌ Cannot DM user (Privacy Settings).')
            admin = self.bot.get_cog('Admin')
            if admin:
                await admin.log_audit(ctx.author, f"❌ Failed DM: Recipient: {user} (Privacy settings).")
        finally:
            try:
                await ctx.message.delete()
                admin = self.bot.get_cog('Admin')
                if admin:
                    await admin.log_audit(ctx.author, f"🗑️ Deleted command: {ctx.message.content}")
            except Exception:
                logger.warning("Suppressed exception", exc_info=True)
