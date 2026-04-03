import asyncio
import logging
import io
import re

import discord
from discord.ext import commands
from typing import Optional
import config
from NightCityBot.utils.permissions import is_fixer, is_ripperdoc, is_store_owner
from NightCityBot.utils import constants
from NightCityBot.utils.helpers import load_json_file, save_json_file
from NightCityBot.utils import db as _db
from NightCityBot.utils.db import attendance_get_user, attendance_append, open_log_add_if_absent
from NightCityBot.utils import config_loader as _cfg

logger = logging.getLogger(__name__)


def _embed_len(e: discord.Embed) -> int:
    """Return the total character count of an embed's text fields."""
    total = len(e.title or "") + len(e.description or "")
    if e.footer and e.footer.text:
        total += len(e.footer.text)
    for f in e.fields:
        total += len(f.name) + len(str(f.value))
    return total


class Admin(commands.Cog):
    """Administrative commands and global error handler."""

    def __init__(self, bot: commands.Bot) -> None:
        """Initialize the admin cog."""
        self.bot = bot
        self._ticket_index: list = []
        self._ticket_index_ids: set = set()

    @commands.Cog.listener()
    async def on_ready(self):
        """Load the ticket index from the database once the bot is connected."""
        await self._load_ticket_index()

    async def _load_ticket_index(self):
        try:
            pool = await _db.get_pool()
            rows = await pool.fetch(
                "SELECT message_id, url, ts::text, title, body FROM ticket_index ORDER BY ts ASC"
            )
            self._ticket_index = [
                {"id": r["message_id"], "url": r["url"], "ts": r["ts"],
                 "title": r["title"], "text": r["body"]}
                for r in rows
            ]
            self._ticket_index_ids = {r["message_id"] for r in rows}
            logger.info("Loaded %d ticket index entries from database.", len(self._ticket_index))
        except Exception as e:
            logger.error("Failed to load ticket index from database: %s", e)

    @staticmethod
    def _embed_to_text(embed: discord.Embed) -> str:
        """Flatten all text in an embed into a single searchable string.

        Discord mentions like <@123456> are kept as-is so user IDs are
        searchable, and the <@> wrapper is also stripped so bare IDs match.
        """
        parts = []
        if embed.title:
            parts.append(embed.title)
        if embed.description:
            parts.append(embed.description)
        for field in embed.fields:
            if field.name:
                parts.append(field.name)
            if field.value:
                parts.append(field.value)
        if embed.footer and embed.footer.text:
            parts.append(embed.footer.text)
        if embed.author and embed.author.name:
            parts.append(embed.author.name)
        raw = " ".join(parts)
        # Also add bare user/role IDs so searching by ID works
        ids = re.findall(r'<[@&#!&]*(\d+)>', raw)
        if ids:
            raw += " " + " ".join(ids)
        return raw

    @staticmethod
    def _is_ticket_embed(message: discord.Message) -> bool:
        """Return True if this message looks like a Tickety ticket event.

        Searches all embed text (title, description, fields, footer, author)
        for ticket-related keywords so different Tickety embed formats are caught.
        Also matches if the message author/webhook name contains 'tickety'.
        """
        # Check the author/webhook name first (fastest path)
        author_name = (getattr(message.author, "display_name", "") or "").lower()
        if "tickety" in author_name or "ticket" in author_name:
            if message.embeds:
                return True

        # Match any embed that contains ticket/transcript keywords anywhere
        keywords = ("ticket", "transcript")
        for embed in message.embeds:
            parts = [
                embed.title or "",
                embed.description or "",
                (embed.footer.text if embed.footer else "") or "",
                (embed.author.name if embed.author else "") or "",
            ]
            for field in embed.fields:
                parts.append(field.name or "")
                parts.append(field.value or "")
            combined = " ".join(parts).lower()
            if any(kw in combined for kw in keywords):
                return True
        return False

    async def _index_message(self, message: discord.Message, pool=None) -> bool:
        """Add a Tickety ticket message to the database index if not already present."""
        if str(message.id) in self._ticket_index_ids:
            return False
        if not message.embeds:
            return False
        if not self._is_ticket_embed(message):
            return False
        text = " ".join(self._embed_to_text(e) for e in message.embeds)
        title = message.embeds[0].title or "(embed)"
        ts_dt = message.created_at  # keep as datetime for asyncpg
        entry = {
            "id": str(message.id),
            "url": message.jump_url,
            "ts": ts_dt.isoformat(),
            "title": title[:120],
            "text": text.lower(),
        }
        try:
            p = pool or await _db.get_pool()
            await p.execute(
                """
                INSERT INTO ticket_index (message_id, url, ts, title, body)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (message_id) DO NOTHING
                """,
                entry["id"], entry["url"], ts_dt, entry["title"], entry["text"],
            )
        except Exception as e:
            logger.error("DB insert failed for message %s: %s", message.id, e)
            return False
        self._ticket_index.append(entry)
        self._ticket_index_ids.add(entry["id"])
        return True

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Auto-index Tickety ticket messages as they arrive."""
        if message.channel.id != config.TICKETY_LOG_CHANNEL_ID:
            return
        if not self._is_ticket_embed(message):
            return
        await self._index_message(message)

    @commands.command()
    @is_fixer()
    async def post(self, ctx, destination: str, *, message: Optional[str] = None):
        """Posts a message to the specified channel or thread."""
        if not ctx.guild:
            await ctx.send("❌ This command can only be used in a server.")
            return
        dest_channel = None

        # Normalize destination string
        destination = destination.strip()
        if destination.startswith("<#") and destination.endswith(">"):
            destination = destination[2:-1]
        if destination.startswith("#"):
            destination = destination[1:]

        if destination.isdigit():
            try:
                dest_channel = await ctx.guild.fetch_channel(int(destination))
            except discord.NotFound:
                dest_channel = None
        else:
            dest_channel = discord.utils.get(ctx.guild.text_channels, name=destination)
            if dest_channel is None:
                for channel in ctx.guild.text_channels:
                    threads = channel.threads
                    dest_channel = discord.utils.get(threads, name=destination)
                    if dest_channel:
                        break

        if dest_channel is None:
            await ctx.send(f"❌ Couldn't find channel/thread '{destination}'.")
            return

        files = [await attachment.to_file() for attachment in ctx.message.attachments]

        if message or files:
            if message and message.strip().startswith("!"):
                command_text = message.strip()
                fake_msg = ctx.message
                fake_msg.content = command_text
                fake_ctx = await self.bot.get_context(fake_msg)
                fake_ctx.channel = dest_channel
                fake_ctx.author = ctx.author
                setattr(fake_ctx, "original_author", ctx.author)
                setattr(fake_ctx, "skip_dm_log", True)

                await self.bot.invoke(fake_ctx)
                await self.log_audit(
                    ctx.author,
                    f"✅ Executed `{command_text}` in {dest_channel.mention}.",
                )
            else:
                await dest_channel.send(content=message, files=files)
                await self.log_audit(
                    ctx.author, f"✅ Posted anonymously to {dest_channel.mention}."
                )
        else:
            await ctx.send("❌ Provide a message or attachment.")
        try:
            await ctx.message.delete()
            await self.log_audit(
                ctx.author, f"🗑️ Deleted command: {ctx.message.content}"
            )
        except Exception:
            logger.warning("Suppressed exception", exc_info=True)
    @commands.command(name="help")
    async def block_help(self, ctx):
        await ctx.send("❌ `!help` is disabled. Use `!helpme` or `!helpfixer` instead.")

    @commands.command(name="helpme")
    async def helpme(self, ctx):
        """Display help for regular users."""
        embed = discord.Embed(
            title="📘 NCRP Bot — Player Help",
            description=(
                "Basic commands for RP, rent, and daily life in Night City.\n\n"
                "**Other help commands:**\n"
                "`!helpguns` — gun store guide\n"
                "`!helpcyberware` — ripperdoc guide\n"
                "`!helpfixer` — fixer & admin tools\n"
                "`!helpadmin` — server administration"
            ),
            color=discord.Color.teal(),
        )

        embed.add_field(
            name="🎲 Dice Rolls",
            value=(
                "`!roll [XdY+Z]` – roll dice, e.g. `!roll 2d6+1`. "
                "Mention another user to roll for them.\n"
                "Rolls made in DMs are recorded in your private log thread."
            ),
            inline=False,
        )

        embed.add_field(
            name="💰 Rent & Cost of Living",
            value=(
                "Everyone pays a **$500/month** baseline fee for survival.\n\n"
                "`!open_shop` — Sundays only. Log a business opening for a cash payout.\n"
                "`!attend` — Sundays only. Verified players earn $250.\n"
                "`!due` — See what you'll owe on the 1st.\n"
                "`!paydue` — Pay your monthly obligations early.\n"
                "`!last_payment` — View your last automated payment."
            ),
            inline=False,
        )

        embed.add_field(
            name="🏖️ Leave of Absence",
            value=(
                "`!start_loa` – pause your fees while away.\n"
                "`!end_loa` – resume costs when you return."
            ),
            inline=False,
        )

        embed.add_field(
            name="🚑 Medical",
            value=(
                "`!call_trauma` – ping the Trauma Team channel.\n"
                "`!paycyberware` – pay your cyberware meds manually."
            ),
            inline=False,
        )

        embed.add_field(
            name="📦 Inventories & Trading",
            value=(
                f"Head to <#{config.PLAYER_HUB_CHANNEL_ID}> for the **Player Hub** — view your inventory, sell to players, give items.\n\n"
                f"**🔫 Gun store** — own a shop? Use the panel in <#{config.GUN_HUB_CHANNEL_ID}>.\n"
                f"**💉 Ripperdoc** — licensed doc? Use the panel in <#{config.RIPPERDOC_HUB_CHANNEL_ID}>."
            ),
            inline=False,
        )

        embed.set_footer(text="Use !roll, pay your rent, stay alive. | !helpguns • !helpcyberware • !helpfixer • !helpadmin")
        await ctx.send(embed=embed)

    @commands.command(name="helpfixer")
    async def helpfixer(self, ctx):
        """Display help for fixers."""

        fields = [
            (
                "🏪 Interactive Hubs",
                "\n".join([
                    f"<#{config.FIXER_HUB_CHANNEL_ID}> – **Fixer panel**: player inventory, items, LOA, store & wholesale management.",
                    f"<#{config.RIPPERDOC_HUB_CHANNEL_ID}> – ripperdoc hub: buy, sell, install, view stock.",
                ]),
            ),
            (
                "✉️ Messaging",
                "\n".join([
                    "`!dm @user <text>` – anonymous DM with attachments.",
                    "`!post <channel|thread> <message>` – send a message in another channel.",
                ]),
            ),
            (
                "📑 RP & Characters",
                "\n".join([
                    "`!start_rp @users...` – create a locked RP channel.",
                    "`!end_rp` – archive the current RP channel.",
                    "`!search_characters <keyword>` – search character sheets.",
                ]),
            ),
            (
                "💵 Economy & Rent",
                "\n".join([
                    "`!event_start` – allow `!attend` / `!open_shop` outside Sunday.",
                    "`!due [@user]` – breakdown of what a user owes.",
                    "`!collect_rent [@user] [-v] [-force]` – run the rent cycle.",
                ]),
            ),
            (
                "💉 Cyberware",
                "\n".join([
                    "`!checkup @user` – remove the checkup role after an exam.",
                    "`!checkup_report` – list checkup/meds status for all CW users.",
                    "`!cyberware_status` – current week status for all CW users.",
                ]),
            ),
        ]

        embeds = []
        current = discord.Embed(
            title="🛠️ NCRP Bot — Fixer Help",
            description=(
                "Most day-to-day work is done through the interactive hubs.\n"
                "Commands below are for things the hubs don't cover."
            ),
            color=discord.Color.purple(),
        )
        for name, value in fields:
            chunks = [value[i : i + 1024] for i in range(0, len(value), 1024)] or [""]
            for i, chunk in enumerate(chunks):
                field_name = name if i == 0 else "\u200b"
                if _embed_len(current) + len(field_name) + len(chunk) > 5800:
                    current.set_footer(text="Fixer tools by MedusaCascade | v1.2")
                    embeds.append(current)
                    current = discord.Embed(
                        title="🛠️ NCRP Bot — Fixer Help (cont.)",
                        color=discord.Color.purple(),
                    )
                current.add_field(name=field_name, value=chunk, inline=False)

        current.set_footer(text="Fixer tools by MedusaCascade | v1.2")
        embeds.append(current)

        for e in embeds:
            await ctx.send(embed=e)

    @commands.command(name="helpadmin")
    async def helpadmin(self, ctx):
        """Display help for administrators."""

        fields = [
            (
                "🏪 Interactive Hubs",
                "\n".join([
                    f"<#{config.ADMIN_HUB_CHANNEL_ID}> – admin panel: add/remove items, reassign, history lookup, wholesale management.",
                    f"<#{config.GUN_HUB_CHANNEL_ID}> – gun store hub: buy, sell, view stock, manage approved buyers.",
                    f"<#{config.RIPPERDOC_HUB_CHANNEL_ID}> – ripperdoc hub: buy, sell, install, view stock.",
                ]),
            ),
            (
                "⚙️ System Control",
                "\n".join([
                    "`!enable_system <name>` / `!disable_system <name>` – toggle subsystems on/off.",
                    "`!system_status` – show current enable/disable flags.",
                    "`!reload_config` – reload config from DB without restarting.",
                    "`!shutdown_bot` – clean shutdown with audit log.",
                    "`!db_health` – database ping, pool stats, failure count.",
                ]),
            ),
            (
                "💵 Rent & Payment",
                "\n".join([
                    "`!collect_rent [@user] [-v] [-force]` – run the monthly rent cycle.",
                    "`!trigger_auto_rent` – run full rent cycle immediately, bypassing the monthly guard.",
                    "`!mark_paid @user [note]` – manually mark a member as paid.",
                    "`!list_deficits` – list members who can't cover upcoming charges.",
                    "`!backup_balances` / `!restore_balances <file>` – snapshot and restore balances.",
                ]),
            ),
            (
                "🔫 Gun Shop / 💉 Cyberware",
                "\n".join([
                    "Gun shop and cyberware admin actions are now handled through",
                    "the interactive hubs: `!gunstore`, `!ripperdoc`, `!fixer`, and `!admin`.",
                ]),
            ),
            (
                "🛠️ Other Admin Tools",
                "\n".join([
                    "`!backfill_logs [limit]` – rebuild attendance/business logs from message history.",
                    "`!reindex_tickets [limit]` – rebuild the ticket search index.",
                    "`!search_tickets <query>` – search tickets by name, user, ID, or text.",
                ]),
            ),
        ]

        embeds = []
        current = discord.Embed(
            title="🛠️ NCRP Bot — Admin Help",
            description=(
                "Most day-to-day management is done through the interactive hubs.\n"
                "Commands below are for things the hubs don't cover."
            ),
            color=discord.Color.dark_gold(),
        )
        for name, value in fields:
            chunks = [value[i : i + 1024] for i in range(0, len(value), 1024)] or [""]
            for i, chunk in enumerate(chunks):
                field_name = name if i == 0 else "\u200b"
                if _embed_len(current) + len(field_name) + len(chunk) > 5800:
                    current.set_footer(text="Admin tools by MedusaCascade | v1.2")
                    embeds.append(current)
                    current = discord.Embed(
                        title="🛠️ NCRP Bot — Admin Help (cont.)",
                        color=discord.Color.dark_gold(),
                    )
                current.add_field(name=field_name, value=chunk, inline=False)

        current.set_footer(text="Admin tools by MedusaCascade | v1.2")
        embeds.append(current)

        for e in embeds:
            await ctx.send(embed=e)

    @commands.command(name="helpguns", aliases=["helpbusiness", "helpshop", "helpstore"])
    @commands.check_any(is_store_owner(), is_fixer(), commands.has_permissions(administrator=True))
    async def helpguns(self, ctx):
        """Display help for the wholesale gun system and gun store owners."""
        embed = discord.Embed(
            title="🔫 NCRP Bot — Gun Shop Guide",
            description=(
                "The corporate wholesaler stocks guns every week. "
                "Store owners buy from the wholesaler, then sell to players at their own markup.\n\n"
                f"**Head to <#{config.GUN_HUB_CHANNEL_ID}>** to use the interactive hub — buy, sell, and view stock all from one place."
            ),
            color=discord.Color.orange(),
        )

        embed.add_field(
            name="📖 How It Works",
            value=(
                "1. **Browse wholesale** — see what's available this week\n"
                "2. **Buy stock** — pay wholesale price, guns go into your store\n"
                "3. **Sell to players** — pick the customer, pick the gun, set your price\n"
                "4. **Customer confirms** — they get a DM to accept or decline\n\n"
                "All of this is handled through the Gun Store hub panel with dropdowns."
            ),
            inline=False,
        )

        embed.add_field(
            name="🔒 Restriction System",
            value=(
                "Guns have a restriction level: **basic**, **controlled**, or **restricted**.\n"
                "- **Basic** — anyone can buy.\n"
                "- **Controlled** — only buyers on your approved list.\n"
                "- **Restricted** — approved list + admin approval per sale.\n\n"
                "Manage your approved list through the Gun Store hub panel."
            ),
            inline=False,
        )

        embed.add_field(
            name="⚠️ Good to Know",
            value=(
                "- Wholesale stock refreshes **weekly** — buy before it's gone.\n"
                "- You set your own sale prices — wholesale cost is your floor.\n"
                "- Restriction levels carry over from the wholesaler.\n"
                f"- Admin tools are available in <#{config.ADMIN_HUB_CHANNEL_ID}>."
            ),
            inline=False,
        )

        embed.set_footer(text="Buy low, sell high, stay strapped. | !helpme • !helpcyberware • !helpfixer • !helpadmin")
        await ctx.send(embed=embed)

    @commands.command(name="helpcyberware", aliases=["helpcw", "helpripper", "helpripperdoc"])
    @commands.check_any(is_ripperdoc(), is_fixer(), commands.has_permissions(administrator=True))
    async def helpcyberware(self, ctx):
        """Display help for the Ripperdoc cyberware shop system."""
        embed = discord.Embed(
            title="🦾 NCRP Bot — Cyberware Shop Guide",
            description=(
                "Ripperdocs buy parts from the corporate supplier at catalogue price, "
                "then install them for patients at their own rate.\n\n"
                f"**Head to <#{config.RIPPERDOC_HUB_CHANNEL_ID}>** to use the interactive hub — buy, sell, install, "
                "and view stock all from one place."
            ),
            color=discord.Color.teal(),
        )

        embed.add_field(
            name="📖 How It Works",
            value=(
                "1. **Browse wholesale** — see what's in rotation this week\n"
                "2. **Buy parts** — pay catalogue price, parts go into your stock\n"
                "3. **Sell or install** — pick the patient, pick the part, set your price\n"
                "4. **Patient confirms** — they get a DM to accept or decline\n\n"
                "All of this is handled through the Ripperdoc hub panel with dropdowns."
            ),
            inline=False,
        )

        embed.add_field(
            name="⚠️ Good to Know",
            value=(
                "- You must buy a part **before** you can sell/install it.\n"
                "- Stock is per-Ripperdoc: your inventory is yours alone.\n"
                "- Wholesale stock rotates every **Sunday**.\n"
                "- Catalogue prices are the supplier floor — charge what you want.\n"
                f"- Admin tools are available in <#{config.ADMIN_HUB_CHANNEL_ID}>."
            ),
            inline=False,
        )

        embed.set_footer(text="Chrome up. | !helpme • !helpguns • !helpfixer • !helpadmin")
        await ctx.send(embed=embed)

    @commands.command(name="shutdown_bot", aliases=["shutdownbot", "forceshutdown"])
    @commands.has_permissions(administrator=True)
    async def shutdown_bot(self, ctx):
        """Force a clean bot shutdown with audit logging."""
        await ctx.send("🛑 Shutdown requested. Closing bot process...")
        wholesaler = self.bot.get_cog("GunsShopCog")
        if wholesaler and hasattr(wholesaler, "emit_inventory_snapshot_audit"):
            await wholesaler.emit_inventory_snapshot_audit("MANUAL_SHUTDOWN", actor=ctx.author)
        await self.log_audit(ctx.author, "🛑 Manual shutdown requested via !shutdown_bot.")
        await self.bot.close()

    @commands.command()
    @commands.has_permissions(administrator=True)
    async def backfill_logs(self, ctx, limit: int = 1000):
        """Rebuild attendance and open shop logs from recent history."""
        attend_channel = ctx.guild.get_channel(config.ATTENDANCE_CHANNEL_ID)
        open_channel = ctx.guild.get_channel(config.BUSINESS_ACTIVITY_CHANNEL_ID)

        attend_added = 0
        open_added = 0

        if isinstance(attend_channel, discord.TextChannel):
            history = [
                m
                async for m in attend_channel.history(limit=limit, oldest_first=True)
            ]
            for idx, msg in enumerate(history):
                if msg.author.bot:
                    continue
                if msg.content.strip().startswith("!attend"):
                    success = False
                    for follow in history[idx + 1 :]:
                        if follow.author == ctx.me and follow.created_at >= msg.created_at:
                            if (
                                follow.content.startswith("✅")
                                and "Attendance logged" in follow.content
                            ):
                                success = True
                            break
                    if success:
                        uid = str(msg.author.id)
                        ts = msg.created_at.replace(microsecond=0).isoformat()
                        existing = await attendance_get_user(uid)
                        if ts not in existing:
                            await attendance_append(uid, ts)
                            attend_added += 1

        if isinstance(open_channel, discord.TextChannel):
            history = [
                m
                async for m in open_channel.history(limit=limit, oldest_first=True)
            ]
            for idx, msg in enumerate(history):
                if msg.author.bot:
                    continue
                if msg.content.strip().startswith(("!open_shop", "!openshop", "!os")):
                    success = False
                    for follow in history[idx + 1 :]:
                        if follow.author == ctx.me and follow.created_at >= msg.created_at:
                            if (
                                follow.content.startswith("✅")
                                and "Business opening logged" in follow.content
                            ):
                                success = True
                            break
                    if success:
                        uid = str(msg.author.id)
                        ts = msg.created_at.replace(microsecond=0).isoformat()
                        added = await open_log_add_if_absent(uid, ts)
                        if added:
                            open_added += 1

        await ctx.send(
            f"✅ Backfilled {attend_added} attendance entries and {open_added} business opens."
        )
        await self.log_audit(
            ctx.author,
            f"Backfilled logs: attend {attend_added}, open {open_added}",
        )

    @commands.group(name="config", invoke_without_command=True)
    @commands.has_permissions(administrator=True)
    async def config_group(self, ctx: commands.Context):
        """Bot configuration commands. Use !config list/get/set/reload."""
        await ctx.send(
            "⚙️ **Config commands:**\n"
            "`!config list` – list all settings\n"
            "`!config get <key>` – get one setting\n"
            "`!config set <key> <value>` – set a value (integer or float)\n"
            "`!config reload` – reload cache from DB without changing values"
        )

    @config_group.command(name="list")
    @commands.has_permissions(administrator=True)
    async def config_list(self, ctx: commands.Context):
        """List all bot_config values."""
        rows = await _db.bot_config_get_all()
        if not rows:
            await ctx.send("⚠️ No config values found in DB.")
            return
        lines = [f"`{k}` = **{v}** _{desc}_" for k, v, desc in rows]
        chunks = []
        chunk = []
        length = 0
        for line in lines:
            if length + len(line) + 1 > 1900:
                chunks.append("\n".join(chunk))
                chunk = []
                length = 0
            chunk.append(line)
            length += len(line) + 1
        if chunk:
            chunks.append("\n".join(chunk))
        for i, text in enumerate(chunks, 1):
            header = f"⚙️ **Bot Config** ({i}/{len(chunks)}):\n" if len(chunks) > 1 else "⚙️ **Bot Config:**\n"
            await ctx.send(header + text)

    @config_group.command(name="get")
    @commands.has_permissions(administrator=True)
    async def config_get(self, ctx: commands.Context, key: str):
        """Get a single config value by key, showing the effective (fallback) value if not in DB."""
        val = await _db.bot_config_get(key)
        if val is None:
            # Show effective fallback from config_loader defaults if available
            all_defaults = _cfg.get_all_defaults()
            if key in all_defaults:
                fallback = all_defaults[key][0]
                await ctx.send(
                    f"⚙️ `{key}` is not in DB — effective value (fallback default): **{fallback}**"
                )
            else:
                await ctx.send(f"❌ Key `{key}` not found in bot_config or defaults.")
        else:
            await ctx.send(f"⚙️ `{key}` = **{val}**")

    @config_group.command(name="set")
    @commands.has_permissions(administrator=True)
    async def config_set(self, ctx: commands.Context, key: str, value: str):
        """Set a config value and reload the in-memory cache."""
        existing = await _db.bot_config_get(key)
        if existing is None:
            await ctx.send(f"❌ Key `{key}` not found. Use `!config list` to see valid keys.")
            return
        expected_type = _cfg.key_value_type(key)
        if expected_type == "int":
            try:
                parsed = int(value)
            except ValueError:
                await ctx.send(
                    f"❌ `{key}` requires an **integer** value (e.g. `500`). "
                    f"`{value}` is not valid."
                )
                return
            if parsed < 0:
                await ctx.send(f"❌ `{key}` must be **0 or greater**. Negative values are not allowed.")
                return
        else:  # "float"
            try:
                parsed = float(value)
            except ValueError:
                await ctx.send(
                    f"❌ `{key}` requires a **decimal** value (e.g. `0.25`). "
                    f"`{value}` is not valid."
                )
                return
            if key.startswith("open_percent"):
                if parsed < 0.0 or parsed > 1.0:
                    await ctx.send(
                        f"❌ `{key}` must be between **0.0** and **1.0** (e.g. 0.25 = 25%). "
                        f"`{value}` is out of range."
                    )
                    return
        ok = await _db.bot_config_set(key, value)
        if not ok:
            await ctx.send(f"⚠️ Database write failed for `{key}`. Value was **not** changed.")
            await self.log_audit(ctx.author, f"config set FAILED {key}={value}")
            return
        await _cfg.reload_config()
        await ctx.send(f"✅ `{key}` updated to **{value}** and cache reloaded.")
        await self.log_audit(ctx.author, f"config set {key}={value}")

    @config_group.command(name="reload")
    @commands.has_permissions(administrator=True)
    async def config_reload(self, ctx: commands.Context):
        """Reload the config cache from DB without changing any values."""
        await _cfg.reload_config()
        await ctx.send("✅ Config cache reloaded from DB.")
        await self.log_audit(ctx.author, "config reload")

    @commands.command(name="reload_config")
    @commands.has_permissions(administrator=True)
    async def reload_config_cmd(self, ctx: commands.Context):
        """Standalone alias: reload the bot config cache from DB."""
        await _cfg.reload_config()
        await ctx.send("✅ Config cache reloaded from DB.")
        await self.log_audit(ctx.author, "reload_config")

    @commands.command(name="migrate_json_store")
    @commands.has_permissions(administrator=True)
    async def migrate_json_store(self, ctx: commands.Context):
        """Migrate all json_store blobs into normalized tables (idempotent, safe to re-run)."""
        from NightCityBot.utils.db import migrate_json_store_blobs

        await ctx.send("⏳ Running `json_store` migration — this may take a moment…")
        try:
            summary = await migrate_json_store_blobs()
        except Exception as exc:
            await ctx.send(f"❌ Migration failed with an unexpected error: `{exc}`")
            logger.error("migrate_json_store command failed", exc_info=True)
            return

        if not summary:
            await ctx.send("ℹ️ `json_store` is empty — nothing to migrate.")
            return

        lines = []
        unknown = []
        total_found = total_inserted = total_skipped = total_errors = 0

        for key, stats in sorted(summary.items()):
            target = stats.get("target")
            if target is None:
                unknown.append(key)
                continue
            found = stats.get("found") or 0
            ins = stats.get("inserted", 0)
            skipped = stats.get("skipped", 0)
            errors = stats.get("errors", 0)
            total_found += found
            total_inserted += ins
            total_skipped += skipped
            total_errors += errors
            status = "✅" if errors == 0 else "⚠️"
            lines.append(
                f"{status} **{key}** → `{target}`: "
                f"{found} found, {ins} inserted, {skipped} skipped"
                + (f", {errors} errors" if errors else "")
            )

        if unknown:
            lines.append(f"⚠️ Unknown keys (skipped): `{'`, `'.join(sorted(unknown))}`")

        lines.append(
            f"\n**Totals** — found: {total_found} | inserted: {total_inserted} | "
            f"skipped: {total_skipped} | errors: {total_errors}"
        )
        lines.append(
            "ℹ️ `json_store` has **not** been dropped — it is kept as an archive. "
            "You may drop it manually once satisfied with the migration."
        )

        text = "\n".join(lines)
        for chunk in [text[i:i+1900] for i in range(0, len(text), 1900)]:
            await ctx.send(chunk)

        await self.log_audit(
            ctx.author,
            f"migrate_json_store: {total_inserted} rows inserted, {total_skipped} skipped, "
            f"{total_errors} errors across {len(summary)} keys",
        )

    @commands.command(name="db_health")
    @commands.has_permissions(administrator=True)
    async def db_health(self, ctx: commands.Context):
        """Show database pool health, ping time, and failure count."""
        from NightCityBot.utils.db import get_pool, db_ping, get_failure_count, get_last_failure_at

        ping_ms = await db_ping()
        failures = get_failure_count()
        last_fail = get_last_failure_at()

        try:
            pool = await get_pool()
            pool_size = pool.get_size()
            pool_idle = pool.get_idle_size()
            pool_min = pool.get_min_size()
            pool_max = pool.get_max_size()
            pool_info = (
                f"Size: {pool_size} (idle: {pool_idle}) | "
                f"Min/Max: {pool_min}/{pool_max}"
            )
        except Exception:
            pool_info = "unavailable"

        if ping_ms is None:
            ping_str = "❌ FAILED"
            status = "🔴"
        elif ping_ms < 50:
            ping_str = f"✅ {ping_ms:.1f} ms"
            status = "🟢"
        else:
            ping_str = f"⚠️ {ping_ms:.1f} ms"
            status = "🟡"

        embed = discord.Embed(
            title=f"{status} Database Health",
            color=(
                discord.Color.green() if ping_ms is not None and ping_ms < 50
                else discord.Color.red() if ping_ms is None
                else discord.Color.gold()
            ),
        )
        last_fail_str = (
            last_fail.strftime("%Y-%m-%d %H:%M:%S UTC") if last_fail else "None"
        )
        embed.add_field(name="Ping", value=ping_str, inline=True)
        embed.add_field(name="Write failures (since startup)", value=str(failures), inline=True)
        embed.add_field(name="Last failure recorded", value=last_fail_str, inline=False)
        embed.add_field(name="Pool", value=pool_info, inline=False)
        await ctx.send(embed=embed)

    @commands.Cog.listener()
    async def on_command_error(self, ctx, error):
        """Global error handler for commands."""
        # Ignore errors triggered by other bots to avoid feedback loops
        if getattr(ctx.author, "bot", False):
            return
        if isinstance(error, commands.CommandNotFound):
            # Ignore specific economy bot commands entirely
            _parts = ctx.message.content.lstrip(self.bot.command_prefix).split()
            if not _parts:
                return
            cmd = _parts[0].lower()
            if cmd in constants.UNBELIEVABOAT_COMMANDS:
                return
            # Otherwise show a basic notice but do not audit
            logger.debug(
                "Unknown command from %s in %s (%s) → %r",
                ctx.author,
                getattr(ctx.channel, "name", ctx.channel.id),
                ctx.channel.id,
                ctx.message.content,
            )
            await ctx.send("❌ Unknown command.")
            return
        elif isinstance(error, commands.CheckFailure):
            reason = str(error) or "Permission denied."
            await ctx.send(f"❌ {reason}")
            await self.log_audit(ctx.author, f"❌ {reason}: {ctx.message.content}")
        elif isinstance(error, commands.UserInputError):
            # Missing args, bad types, too many args, etc. — show usage hint, no alert.
            hint = ""
            if ctx.command:
                sig = ctx.command.signature
                name = ctx.command.qualified_name
                if sig:
                    hint = f"\n📋 Usage: `!{name} {sig}`"
                else:
                    hint = f"\n📋 Command: `!{name}`"
            await ctx.send(f"⚠️ {str(error)}{hint}")
        else:
            await ctx.send(f"⚠️ Error: {str(error)}")
            await self.log_audit(
                ctx.author, f"⚠️ Error: {ctx.message.content} → {str(error)}"
            )
            channel_info = getattr(ctx.channel, "name", str(getattr(ctx.channel, "id", "?")))
            await self._alert_report_user(
                f"⚠️ **Unexpected command error**\n"
                f"User: {ctx.author} (`{ctx.author.id}`)\n"
                f"Channel: #{channel_info}\n"
                f"Command: `{ctx.message.content}`\n"
                f"Error: `{type(error).__name__}: {error}`"
            )

    async def _alert_report_user(self, message: str) -> None:
        """DM the configured REPORT_USER_ID with an alert message."""
        user_id = getattr(config, "REPORT_USER_ID", 0)
        if not user_id:
            return
        user = self.bot.get_user(user_id)
        if user is None:
            try:
                user = await self.bot.fetch_user(user_id)
            except Exception:
                logger.warning("_alert_report_user: could not fetch REPORT_USER_ID %s", user_id)
                return
        try:
            await user.send(message)
        except Exception:
            logger.warning("_alert_report_user: DM to %s failed", user_id, exc_info=True)

    @commands.command(name="export_threads", aliases=["exportthreads"])
    @is_fixer()
    async def export_threads(self, ctx: commands.Context, channel_input: str):
        """Export all threads from a channel into an HTML file.

        Usage: ``!export_threads #channel`` or ``!export_threads <channel_id>``
        """
        channel_id = None
        import re as _re
        match = _re.match(r"<#(\d+)>", channel_input)
        if match:
            channel_id = int(match.group(1))
        elif channel_input.isdigit():
            channel_id = int(channel_input)

        if not channel_id:
            await ctx.send("❌ Provide a channel mention or ID. Example: `!export_threads #general`")
            return

        channel = (ctx.guild.get_channel(channel_id) if ctx.guild else None) or self.bot.get_channel(channel_id)
        if not channel:
            await ctx.send("❌ Channel not found in this server.")
            return

        is_forum = isinstance(channel, discord.ForumChannel)
        is_text = isinstance(channel, discord.TextChannel)
        if not is_forum and not is_text:
            await ctx.send("❌ That channel type doesn't support threads.")
            return

        try:
            status_msg = await ctx.send(f"⏳ Exporting threads from **{channel.name}**… this may take a while.")
        except Exception:
            status_msg = None

        async def _update_status(content: str):
            if status_msg is not None:
                try:
                    await status_msg.edit(content=content)
                except Exception:
                    logger.warning("Suppressed exception", exc_info=True)
        threads = []
        if is_forum:
            for t in channel.threads:
                threads.append(t)
            async for t in channel.archived_threads(limit=None):
                if t not in threads:
                    threads.append(t)
        else:
            for t in channel.threads:
                threads.append(t)
            async for t in channel.archived_threads(limit=None):
                if t not in threads:
                    threads.append(t)

        if not threads:
            await _update_status(f"ℹ️ No threads found in **{channel.name}**.")
            if not status_msg:
                await ctx.send(f"ℹ️ No threads found in **{channel.name}**.")
            return

        await _update_status(f"⏳ Found {len(threads)} thread(s). Reading messages…")

        thread_data = []
        for i, thread in enumerate(threads, 1):
            if i % 10 == 0:
                await _update_status(f"⏳ Processing thread {i}/{len(threads)}…")

            messages = []
            try:
                async for msg in thread.history(limit=None, oldest_first=True):
                    attachments = []
                    for a in msg.attachments:
                        attachments.append({"name": a.filename, "url": a.url})
                    embeds_list = []
                    for emb in msg.embeds:
                        embeds_list.append({
                            "title": emb.title or "",
                            "description": emb.description or "",
                        })
                    messages.append({
                        "author": getattr(msg.author, "display_name", str(msg.author)),
                        "author_id": msg.author.id,
                        "content": msg.content or "",
                        "timestamp": msg.created_at.strftime("%Y-%m-%d %H:%M:%S UTC"),
                        "attachments": attachments,
                        "embeds": embeds_list,
                    })
            except discord.Forbidden:
                messages.append({
                    "author": "System",
                    "author_id": 0,
                    "content": "[Could not read thread — missing permissions]",
                    "timestamp": "",
                    "attachments": [],
                    "embeds": [],
                })

            tags = []
            if hasattr(thread, "applied_tags"):
                tags = [t.name for t in thread.applied_tags]

            thread_data.append({
                "name": thread.name,
                "id": thread.id,
                "tags": tags,
                "message_count": len(messages),
                "messages": messages,
                "archived": thread.archived,
                "created_at": thread.created_at.strftime("%Y-%m-%d %H:%M:%S UTC") if thread.created_at else "",
            })

        html = self._build_export_html(channel.name, thread_data)

        import io as _io
        buf = _io.BytesIO(html.encode("utf-8"))
        filename = f"{channel.name}_threads_export.html"
        file = discord.File(buf, filename=filename)

        await _update_status(f"✅ Exported {len(threads)} thread(s) from **{channel.name}**.")
        await ctx.send(file=file)

    @staticmethod
    def _build_export_html(channel_name: str, threads: list) -> str:
        from html import escape
        parts = [
            "<!DOCTYPE html>",
            "<html lang='en'>",
            "<head>",
            "<meta charset='UTF-8'>",
            f"<title>Threads — #{escape(channel_name)}</title>",
            "<style>",
            "  * { box-sizing: border-box; margin: 0; padding: 0; }",
            "  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;",
            "         background: #1a1a2e; color: #e0e0e0; padding: 20px; line-height: 1.5; }",
            "  h1 { color: #00d4ff; margin-bottom: 10px; font-size: 1.8em; }",
            "  .summary { color: #888; margin-bottom: 30px; }",
            "  .thread { background: #16213e; border: 1px solid #0f3460; border-radius: 8px;",
            "            margin-bottom: 20px; overflow: hidden; }",
            "  .thread-header { background: #0f3460; padding: 12px 16px; cursor: pointer;",
            "                   display: flex; justify-content: space-between; align-items: center; }",
            "  .thread-header:hover { background: #1a4080; }",
            "  .thread-title { font-weight: bold; color: #00d4ff; font-size: 1.1em; }",
            "  .thread-meta { font-size: 0.85em; color: #888; }",
            "  .tag { display: inline-block; background: #533483; color: #e0e0e0; padding: 2px 8px;",
            "         border-radius: 4px; font-size: 0.75em; margin-left: 6px; }",
            "  .thread-body { display: none; padding: 0; }",
            "  .thread.open .thread-body { display: block; }",
            "  .message { padding: 10px 16px; border-bottom: 1px solid #0f3460; }",
            "  .message:last-child { border-bottom: none; }",
            "  .msg-header { display: flex; gap: 10px; align-items: baseline; margin-bottom: 4px; }",
            "  .msg-author { font-weight: bold; color: #e94560; }",
            "  .msg-time { font-size: 0.8em; color: #666; }",
            "  .msg-content { white-space: pre-wrap; word-wrap: break-word; }",
            "  .attachment { margin-top: 6px; }",
            "  .attachment a { color: #00d4ff; text-decoration: none; }",
            "  .attachment a:hover { text-decoration: underline; }",
            "  .embed-block { margin-top: 6px; border-left: 3px solid #533483; padding: 6px 10px;",
            "                 background: #1a1a3e; border-radius: 4px; }",
            "  .embed-title { font-weight: bold; color: #00d4ff; }",
            "  .archived-badge { background: #e94560; color: #fff; padding: 2px 8px;",
            "                    border-radius: 4px; font-size: 0.75em; }",
            "  .toc { background: #16213e; border: 1px solid #0f3460; border-radius: 8px;",
            "         padding: 16px; margin-bottom: 30px; }",
            "  .toc h2 { color: #00d4ff; margin-bottom: 10px; }",
            "  .toc a { color: #e0e0e0; text-decoration: none; display: block; padding: 3px 0; }",
            "  .toc a:hover { color: #00d4ff; }",
            "  .search-box { width: 100%; padding: 10px; margin-bottom: 20px; border: 1px solid #0f3460;",
            "                border-radius: 6px; background: #16213e; color: #e0e0e0; font-size: 1em; }",
            "  .search-box::placeholder { color: #666; }",
            "  .hidden { display: none !important; }",
            "</style>",
            "</head>",
            "<body>",
            f"<h1>Threads — #{escape(channel_name)}</h1>",
            f"<p class='summary'>{len(threads)} thread(s), "
            f"{sum(t['message_count'] for t in threads)} total message(s)</p>",
            "<input type='text' class='search-box' placeholder='Search threads…' oninput='filterThreads(this.value)'>",
            "<div class='toc'><h2>Table of Contents</h2>",
        ]

        for i, t in enumerate(threads):
            badge = " [archived]" if t["archived"] else ""
            parts.append(
                f"  <a href='#thread-{i}' onclick=\"toggleThread('t{i}')\">"
                f"{escape(t['name'])} ({t['message_count']} msgs){badge}</a>"
            )
        parts.append("</div>")

        for i, t in enumerate(threads):
            tags_html = "".join(f"<span class='tag'>{escape(tag)}</span>" for tag in t["tags"])
            archived = "<span class='archived-badge'>Archived</span> " if t["archived"] else ""
            parts.append(f"<div class='thread' id='t{i}' data-name='{escape(t['name']).lower()}'>")
            parts.append(f"  <div class='thread-header' id='thread-{i}' onclick=\"toggleThread('t{i}')\">")
            parts.append(f"    <span><span class='thread-title'>{escape(t['name'])}</span>{tags_html}</span>")
            parts.append(f"    <span class='thread-meta'>{archived}{t['message_count']} messages · {t['created_at']}</span>")
            parts.append("  </div>")
            parts.append("  <div class='thread-body'>")

            for msg in t["messages"]:
                parts.append("    <div class='message'>")
                parts.append(
                    f"      <div class='msg-header'>"
                    f"<span class='msg-author'>{escape(msg['author'])}</span>"
                    f"<span class='msg-time'>{escape(msg['timestamp'])}</span></div>"
                )
                if msg["content"]:
                    parts.append(f"      <div class='msg-content'>{escape(msg['content'])}</div>")
                for att in msg["attachments"]:
                    parts.append(
                        f"      <div class='attachment'>📎 <a href='{escape(att['url'])}' "
                        f"target='_blank'>{escape(att['name'])}</a></div>"
                    )
                for emb in msg["embeds"]:
                    parts.append("      <div class='embed-block'>")
                    if emb["title"]:
                        parts.append(f"        <div class='embed-title'>{escape(emb['title'])}</div>")
                    if emb["description"]:
                        parts.append(f"        <div>{escape(emb['description'])}</div>")
                    parts.append("      </div>")
                parts.append("    </div>")

            parts.append("  </div>")
            parts.append("</div>")

        parts.append("<script>")
        parts.append("function toggleThread(id) {")
        parts.append("  document.getElementById(id).classList.toggle('open');")
        parts.append("}")
        parts.append("function filterThreads(q) {")
        parts.append("  q = q.toLowerCase();")
        parts.append("  document.querySelectorAll('.thread').forEach(el => {")
        parts.append("    const name = el.dataset.name || '';")
        parts.append("    const text = el.textContent.toLowerCase();")
        parts.append("    el.classList.toggle('hidden', q && !text.includes(q));")
        parts.append("  });")
        parts.append("}")
        parts.append("</script>")
        parts.append("</body></html>")
        return "\n".join(parts)

    @commands.command(name="reindex_tickets", aliases=["reindextickets"])
    @commands.has_permissions(administrator=True)
    async def reindex_tickets(self, ctx, limit: int = 500000):
        """Scan the Tickety log channel and rebuild the local search index.

        Run this once to seed the index from history. New tickets are indexed
        automatically going forward. Pass a limit to cap how many messages to scan,
        or pass 0 for no limit (full history sweep).
        """
        channel = ctx.guild.get_channel(config.TICKETY_LOG_CHANNEL_ID)
        if channel is None:
            await ctx.send("⚠️ TICKETY_LOG_CHANNEL_ID is not set or channel not found.")
            return

        actual_limit = limit if limit > 0 else None
        limit_str = f"up to {limit:,}" if actual_limit else "all"
        status_msg = await ctx.send(
            f"⏳ Reindexing `#{channel.name}` ({limit_str} messages, newest-first) — "
            f"this runs in the background. I'll post progress every 10,000 messages."
        )
        added = 0
        scanned = 0
        pool = await _db.get_pool()
        # Fetch newest-first so recent tickets are always indexed even on huge channels.
        # The DB uses INSERT ... ON CONFLICT DO NOTHING so insertion order doesn't matter.
        # Sleep 1 s every 100 messages to stay well under Discord rate limits.
        async for message in channel.history(limit=actual_limit, oldest_first=False):
            scanned += 1
            if await self._index_message(message, pool=pool):
                added += 1
            if scanned % 100 == 0:
                await asyncio.sleep(1)
            if scanned % 10000 == 0:
                await ctx.send(
                    f"⏳ Still going… scanned {scanned:,} messages, found {added:,} tickets so far."
                )

        if status_msg:
            try:
                await status_msg.delete()
            except discord.HTTPException:
                pass

        await ctx.send(
            f"✅ Reindex complete — scanned {scanned:,} messages, "
            f"added {added:,} new entries ({len(self._ticket_index):,} total in index)."
        )

    @commands.command(name="ticket_debug", aliases=["ticketdebug"])
    @commands.has_permissions(administrator=True)
    async def ticket_debug(self, ctx, index: int = 0):
        """Show the raw stored text for a ticket index entry.

        Pass an index (0 = most recent, 1 = second most recent, etc.).
        Useful for diagnosing why searches aren't matching.
        """
        if not self._ticket_index:
            await ctx.send("Index is empty. Run `!reindex_tickets` first.")
            return
        entries = list(reversed(self._ticket_index))
        if index >= len(entries):
            await ctx.send(f"Index only has {len(entries)} entries.")
            return
        e = entries[index]
        text_preview = e.get("text", "")[:800]
        embed = discord.Embed(
            title=f"🔍 Index entry [{index}] — {e.get('title', '?')}",
            description=f"**URL:** {e.get('url')}\n**Date:** {e.get('ts', '')[:10]}\n\n**Stored text:**\n```\n{text_preview}\n```",
            color=discord.Color.orange(),
        )
        embed.set_footer(text=f"Total index size: {len(self._ticket_index):,} entries")
        await ctx.send(embed=embed)

    @commands.command(name="ticket_channel_preview", aliases=["ticketchannelpreview"])
    @commands.has_permissions(administrator=True)
    async def ticket_channel_preview(self, ctx, count: int = 5):
        """Show embed info for the most recent messages in the ticket log channel.

        Use this to diagnose why !reindex_tickets isn't finding entries — it shows
        the raw author, embed titles, and whether each message would be indexed.
        """
        channel = ctx.guild.get_channel(config.TICKETY_LOG_CHANNEL_ID)
        if channel is None:
            await ctx.send("⚠️ TICKETY_LOG_CHANNEL_ID not set or channel not found.")
            return
        count = max(1, min(count, 20))
        lines = [f"**Last {count} messages in {channel.mention}:**"]
        async for msg in channel.history(limit=count, oldest_first=False):
            would_index = self._is_ticket_embed(msg)
            author_name = getattr(msg.author, "display_name", str(msg.author))
            embed_titles = [f"`{e.title or '(no title)'}`" for e in msg.embeds] or ["*(no embeds)*"]
            status = "✅ would index" if would_index else "❌ skipped"
            lines.append(
                f"• {status} | author=**{author_name}** | embeds={', '.join(embed_titles)}"
            )
        await ctx.send("\n".join(lines)[:1900])

    @commands.command(name="ticket_scan", aliases=["ticketscan"])
    @commands.has_permissions(administrator=True)
    async def ticket_scan(self, ctx, scan_limit: int = 2000):
        """Scan back through the log channel and show the first embed from each unique bot.

        Searches up to scan_limit messages (default 2000) and shows one sample embed
        per unique author so you can identify the Tickety embed format.
        Usage: !ticket_scan [limit]
        """
        channel = ctx.guild.get_channel(config.TICKETY_LOG_CHANNEL_ID)
        if channel is None:
            await ctx.send("⚠️ TICKETY_LOG_CHANNEL_ID not set or channel not found.")
            return

        await ctx.send(
            f"🔍 Scanning up to {scan_limit:,} messages in {channel.mention} for embeds…"
        )

        seen_authors: dict = {}  # author_name -> first embed sample
        scanned = 0
        async for msg in channel.history(limit=scan_limit, oldest_first=False):
            scanned += 1
            if not msg.embeds:
                continue
            author_name = getattr(msg.author, "display_name", str(msg.author))
            if author_name in seen_authors:
                continue
            e = msg.embeds[0]
            # Build a compact sample of the embed's text content
            parts = []
            if e.title:
                parts.append(f"title=`{e.title[:60]}`")
            if e.description:
                parts.append(f"desc=`{e.description[:80]}`")
            if e.author and e.author.name:
                parts.append(f"embed_author=`{e.author.name[:60]}`")
            if e.fields:
                parts.append(f"fields=[{', '.join(f.name[:30] for f in e.fields[:3])}]")
            if e.footer and e.footer.text:
                parts.append(f"footer=`{e.footer.text[:60]}`")
            seen_authors[author_name] = " | ".join(parts) or "*(empty embed)*"

        if not seen_authors:
            await ctx.send(f"No embed messages found in the last {scanned:,} messages.")
            return

        lines = [f"**Unique embed authors in last {scanned:,} messages of {channel.mention}:**"]
        for author, sample in seen_authors.items():
            would = "✅" if any(
                kw in sample.lower() for kw in ("ticket", "transcript")
            ) else "❌"
            lines.append(f"{would} **{author}**: {sample}")
        await ctx.send("\n".join(lines)[:1900])

    @commands.command(name="search_tickets", aliases=["searchtickets", "ticketsearch"])
    @commands.has_permissions(administrator=True)
    async def search_tickets(self, ctx, *, query: str):
        """Search the local Tickety index instantly.

        Usage: !search_tickets <query>
        Searches ticket names, user names, ticket IDs, reasons — anything
        Tickety puts in its embeds. Run !reindex_tickets first to build the index
        from history; new tickets are indexed automatically.
        """
        q = query.strip().lower()
        if not q:
            await ctx.send("Please provide a search term.")
            return

        if not self._ticket_index:
            await ctx.send(
                "⚠️ The ticket index is empty. Run `!reindex_tickets` to build it from channel history."
            )
            return

        # Search newest-first (index is oldest-first, so reverse it)
        matches = [
            e for e in reversed(self._ticket_index)
            if q in e.get("text", "")
        ]

        if not matches:
            await ctx.send(
                f"❌ No tickets matched **{query}** in the index "
                f"({len(self._ticket_index):,} entries searched)."
            )
            return

        total = len(matches)
        header = (
            f"Found **{total}** match{'es' if total != 1 else ''} "
            f"({len(self._ticket_index):,} tickets indexed)"
        )

        # Build all result lines then paginate into embeds (max ~4000 chars each).
        lines = []
        for e in matches:
            ts_str = e["ts"][:10]  # YYYY-MM-DD
            title = e["title"][:80]
            lines.append(f"[{ts_str}]({e['url']}) — {title}")

        # Split lines into field-sized chunks (≤1024 chars each).
        fields: list[str] = []
        chunk = ""
        for line in lines:
            if len(chunk) + len(line) + 1 > 1024:
                fields.append(chunk.rstrip("\n"))
                chunk = line + "\n"
            else:
                chunk += line + "\n"
        if chunk:
            fields.append(chunk.rstrip("\n"))

        # Pack fields into embeds (≤5 fields each to stay safely under the 6000-char limit).
        FIELDS_PER_EMBED = 5
        pages = [fields[i:i + FIELDS_PER_EMBED] for i in range(0, len(fields), FIELDS_PER_EMBED)]
        for page_num, page_fields in enumerate(pages, 1):
            embed = discord.Embed(
                title=f"🎫 Ticket Search: {query}" + (f" (page {page_num}/{len(pages)})" if len(pages) > 1 else ""),
                description=header if page_num == 1 else f"*(continued — page {page_num}/{len(pages)})*",
                color=discord.Color.blurple(),
            )
            for field_text in page_fields:
                embed.add_field(name="Matches", value=field_text, inline=False)
            await ctx.send(embed=embed)

    async def log_audit(self, user, action_desc):
        """Log an audit entry to the audit channel."""
        audit_channel = self.bot.get_channel(config.AUDIT_LOG_CHANNEL_ID)

        if isinstance(audit_channel, discord.TextChannel):
            embed = discord.Embed(title="📝 Audit Log", color=discord.Color.blue())
            embed.add_field(name="User", value=f"{user} ({user.id})", inline=False)
            chunks = [action_desc[i : i + 1024] for i in range(0, len(action_desc), 1024)] or [""]
            embed.add_field(name="Action", value=chunks[0], inline=False)
            for chunk in chunks[1:]:
                embed.add_field(name="​", value=chunk, inline=False)
            await audit_channel.send(embed=embed)
        else:
            logger.warning(
                "Skipped audit log: channel %s is not a TextChannel",
                config.AUDIT_LOG_CHANNEL_ID,
            )
        logger.info("AUDIT %s: %s", user, action_desc)
