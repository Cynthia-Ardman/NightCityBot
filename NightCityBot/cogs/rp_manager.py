import re
import logging
import asyncio
import discord
from discord.ext import commands
from typing import Optional, List, cast
from NightCityBot.utils.permissions import is_fixer
from NightCityBot.utils.helpers import build_channel_name
import config

logger = logging.getLogger(__name__)


class RPManager(commands.Cog):
    """Cog for managing temporary RP channels."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author == self.bot.user or message.author.bot:
            return
        if isinstance(message.channel, discord.TextChannel) and message.channel.name.startswith("text-rp-"):
            if message.content.strip().startswith("!"):
                # Let the bot's normal processing run the command.
                # We only delete the message here to keep the RP channel clean.
                # Deleting is deferred briefly so the command has time to run
                # first (e.g. end_rp may delete the channel — if so, the
                # message is already gone and we suppress the 404 quietly).
                admin = self.bot.get_cog('Admin')
                await asyncio.sleep(0.5)
                try:
                    await message.delete()
                    if admin:
                        await admin.log_audit(message.author, f"🗑️ Deleted command in RP channel: {message.content}")
                except discord.NotFound:
                    pass  # Channel or message already gone (e.g. end_rp deleted the channel)
                except Exception:
                    logger.warning("Suppressed exception while deleting RP command message", exc_info=True)
                return

    @commands.command(
        aliases=["startrp", "rp_start", "rpstart"]
    )
    @commands.check_any(is_fixer(), commands.has_permissions(administrator=True))
    async def start_rp(self, ctx, *user_identifiers: str):
        """Starts a private RP channel for the mentioned users."""
        if not ctx.guild:
            await ctx.send("❌ This command can only be used in a server.")
            return
        guild = ctx.guild
        users = []

        for identifier in user_identifiers:
            if identifier.isdigit():
                member = guild.get_member(int(identifier))
            else:
                match = re.findall(r"<@!?(\d+)>", identifier)
                member = guild.get_member(int(match[0])) if match else None
            if member:
                users.append(member)

        if not users:
            await ctx.send("❌ Could not resolve any users.")
            admin = self.bot.get_cog('Admin')
            if admin:
                await admin.log_audit(ctx.author, "❌ start_rp failed: no users resolved")
            try:
                await ctx.message.delete()
                if admin:
                    await admin.log_audit(ctx.author, f"🗑️ Deleted command: {ctx.message.content}")
            except Exception:
                logger.warning("Suppressed exception", exc_info=True)
            return

        _fallback_cat_id = ctx.channel.category.id if getattr(ctx.channel, "category", None) else None
        target_category = ctx.guild.get_channel(getattr(config, "RP_IC_CATEGORY_ID", _fallback_cat_id))
        if not isinstance(target_category, discord.CategoryChannel):
            target_category = ctx.channel.category
        channel = await self.create_group_rp_channel(ctx.guild, users + [ctx.author], target_category)
        if not channel:
            await ctx.send("❌ Failed to create RP channel.")
            admin = self.bot.get_cog('Admin')
            if admin:
                await admin.log_audit(ctx.author, "❌ Failed to create RP channel.")
            try:
                await ctx.message.delete()
                if admin:
                    await admin.log_audit(ctx.author, f"🗑️ Deleted command: {ctx.message.content}")
            except Exception:
                logger.warning("Suppressed exception", exc_info=True)
            return None

        mentions = " ".join(user.mention for user in users)
        fixer_role = ctx.guild.get_role(config.FIXER_ROLE_ID)
        fixer_mention = fixer_role.mention if fixer_role else ""

        await channel.send(f"✅ RP session created! {mentions} {fixer_mention}")
        admin = self.bot.get_cog('Admin')
        if admin:
            await admin.log_audit(ctx.author, f"✅ RP channel created: {channel.mention}")
        try:
            await ctx.message.delete()
            if admin:
                await admin.log_audit(ctx.author, f"🗑️ Deleted command: {ctx.message.content}")
        except Exception:
            logger.warning("Suppressed exception", exc_info=True)
        return channel

    @commands.command(
        aliases=["endrp", "rp_end", "rpend"]
    )
    @is_fixer()
    async def end_rp(self, ctx):
        """Ends the RP session in the current channel."""
        if not ctx.guild:
            await ctx.send("❌ This command can only be used in a server.")
            return
        channel = ctx.channel
        if not getattr(channel, "name", "").startswith("text-rp-"):
            await ctx.send("❌ This command can only be used in an RP session channel.")
            return

        await ctx.send("📝 Ending RP session, logging contents and deleting channel...")
        logger.debug("end_rp invoked by %s in %s", ctx.author, channel)
        await self.end_rp_session(channel)

    async def create_group_rp_channel(
            self,
            guild: discord.Guild,
            users: list[discord.Member],
            category: Optional[discord.CategoryChannel] = None
    ):
        """Creates a private RP channel for a group of users."""
        usernames = [(user.name, user.id) for user in users]
        channel_name = build_channel_name(usernames)

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True),
        }

        for user in users:
            overwrites[user] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

        fixer_role = guild.get_role(config.FIXER_ROLE_ID)
        if fixer_role:
            overwrites[fixer_role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

        try:
            return await guild.create_text_channel(
                name=channel_name,
                overwrites=overwrites,
                category=category,
                reason="Creating private RP group channel"
            )
        except discord.Forbidden:
            logger.warning("Missing permissions to create RP channel")
            return None
        except discord.HTTPException as e:
            logger.exception("Failed to create RP channel: %s", e)
            return None

    async def end_rp_session(
            self, channel: discord.TextChannel
    ) -> Optional[discord.Thread]:
        """Archive and end an RP session and return the created log thread."""
        logger.info("end_rp_session started for channel %s", channel)
        forum_id = getattr(config, "RP_LOG_FORUM_CHANNEL_ID", 0) or getattr(config, "GROUP_AUDIT_LOG_CHANNEL_ID", 0)
        log_channel = channel.guild.get_channel(forum_id)
        logger.info("RP log forum resolved as %s (id=%s, type=%s)", log_channel, forum_id, type(log_channel).__name__)
        try:
            if not isinstance(log_channel, discord.ForumChannel):
                ch_type = type(log_channel).__name__ if log_channel else "not found"
                await channel.send(
                    "❌ **Cannot archive RP session** — the RP log forum channel is not configured correctly "
                    f"(expected ForumChannel, got `{ch_type}` for ID `{forum_id}`).\n"
                    "**This channel has NOT been deleted.** Contact an admin to set `RP_LOG_FORUM_CHANNEL_ID` "
                    "in config to the correct RP log forum channel ID."
                )
                logger.error(
                    "end_rp: RP log forum ID %s resolved to %s, expected ForumChannel. "
                    "Channel %s was NOT deleted to prevent data loss.",
                    forum_id, ch_type, channel,
                )
                return None

            participants = channel.name.replace("text-rp-", "").split("-")
            thread_name = "GroupRP-" + "-".join(participants)

            logger.info("creating log thread %s in %s", thread_name, log_channel)
            created = await log_channel.create_thread(
                name=thread_name,
                content=f"📘 RP log for `{channel.name}`"
            )

            log_thread = created.thread if hasattr(created, "thread") else created
            log_thread = cast(discord.Thread, log_thread)

            buffer = ""
            async for msg in channel.history(limit=None, oldest_first=True):
                ts = msg.created_at.strftime("%Y-%m-%d %H:%M:%S")
                content = msg.content or "*(No text content)*"
                entry = f"[{ts}] 📥 **Received from {msg.author.display_name}**:\n{content}"

                if msg.attachments:
                    for attachment in msg.attachments:
                        entry += f"\n📎 Attachment: {attachment.url}"

                if len(entry) > 1900:
                    chunks = [entry[i:i + 1900] for i in range(0, len(entry), 1900)]
                    for chunk in chunks:
                        if buffer:
                            await log_thread.send(buffer)
                            buffer = ""
                            await asyncio.sleep(1)
                        await log_thread.send(chunk)
                        await asyncio.sleep(1)
                    continue

                if len(buffer) + len(entry) + 1 > 1900:
                    await log_thread.send(buffer)
                    buffer = entry
                    await asyncio.sleep(1)
                else:
                    buffer += entry + "\n"

            if buffer:
                await log_thread.send(buffer)

            logger.info("deleting RP channel %s after logging", channel)
            await channel.delete(reason="RP session ended and logged.")
            return log_thread
        except Exception as e:
            logger.exception("Failed to end RP session: %s", e)
            await channel.send(f"⚠️ Error ending RP: {e}")
            return None
