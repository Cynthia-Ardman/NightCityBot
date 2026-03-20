import asyncio
import logging
import io
import contextlib

import discord
from discord.ext import commands
from typing import Optional
import config
from NightCityBot.utils.permissions import is_fixer
from NightCityBot.utils import constants
from NightCityBot.utils import startup_checks
from NightCityBot.utils.helpers import load_json_file, save_json_file

logger = logging.getLogger(__name__)


class Admin(commands.Cog):
    """Administrative commands and global error handler."""

    def __init__(self, bot: commands.Bot) -> None:
        """Initialize the admin cog."""
        self.bot = bot
        self._ticket_index: list = []
        self._ticket_index_ids: set = set()
        config.TICKET_INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)

    @commands.Cog.listener()
    async def on_ready(self):
        """Load the ticket index from disk once the bot is connected."""
        await self._load_ticket_index()

    async def _load_ticket_index(self):
        data = await load_json_file(config.TICKET_INDEX_FILE, default=[])
        if isinstance(data, list):
            self._ticket_index = data
            self._ticket_index_ids = {e["id"] for e in data if "id" in e}

    async def _save_ticket_index(self):
        await save_json_file(config.TICKET_INDEX_FILE, self._ticket_index)

    @staticmethod
    def _embed_to_text(embed: discord.Embed) -> str:
        """Flatten all text in an embed into a single searchable string.

        Discord mentions like <@123456> are kept as-is so user IDs are
        searchable, and the <@> wrapper is also stripped so bare IDs match.
        """
        import re as _re
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
        ids = _re.findall(r'<[@&#!&]*(\d+)>', raw)
        if ids:
            raw += " " + " ".join(ids)
        return raw

    async def _index_message(self, message: discord.Message, save: bool = True):
        """Add a message's embeds to the ticket index if not already present."""
        if str(message.id) in self._ticket_index_ids:
            return False
        if not message.embeds:
            return False
        text = " ".join(self._embed_to_text(e) for e in message.embeds)
        title = message.embeds[0].title or "(embed)"
        entry = {
            "id": str(message.id),
            "url": message.jump_url,
            "ts": message.created_at.isoformat(),
            "title": title[:120],
            "text": text.lower(),
        }
        self._ticket_index.append(entry)
        self._ticket_index_ids.add(str(message.id))
        if save:
            await self._save_ticket_index()
        return True

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Auto-index Tickety embed messages as they arrive."""
        if message.channel.id != config.TICKETY_LOG_CHANNEL_ID:
            return
        if not message.embeds:
            return
        await self._index_message(message)

    @commands.command()
    @is_fixer()
    async def post(self, ctx, destination: str, *, message: Optional[str] = None):
        """Posts a message to the specified channel or thread."""
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
            pass

    @commands.command(name="help")
    async def block_help(self, ctx):
        await ctx.send("❌ `!help` is disabled. Use `!helpme` or `!helpfixer` instead.")

    @commands.command(name="helpme")
    async def helpme(self, ctx):
        """Display help for regular users."""
        embed = discord.Embed(
            title="📘 NCRP Bot — Player Help",
            description="Basic commands for RP, rent, and rolling dice. Use `!helpfixer` if you're a Fixer, or `!helpbusiness` if you run a gun store.",
            color=discord.Color.teal(),
        )

        embed.add_field(
            name="🎲 Dice Rolls",
            value=(
                "`!roll [XdY+Z]` – roll dice using standard notation, e.g. `!roll 2d6+1`. "
                "Mention another user to roll for them.\n"
                "Rolls made in DMs are recorded in your private log thread."
            ),
            inline=False,
        )

        embed.add_field(
            name="💰 Rent & Cost of Living",
            value=(
                "Everyone pays a **$500/month** baseline fee for survival (food, water, etc).\n"
                "Even if you don't have a house or business — you're still eating Prepack.\n\n"
                "`!open_shop` (aliases: !openshop, !os) — Sundays only\n"
                "→ Log up to 4 openings per month. Each opening grants an immediate cash payout based on your business tier.\n"
                "→ Requires a Business role.\n"
                "`!attend` — Sundays only\n"
                "→ Verified players earn $250 every week they attend.\n"
                "`!due` — Estimate what you'll owe on the 1st.\n"
                "`!paydue [-v]` — pay your monthly obligations early.\n"
                "`!last_payment` — view the details of your last automated payment."
            ),
            inline=False,
        )

        embed.add_field(
            name="🏖️ Leave of Absence",
            value=(
                "`!start_loa` (aliases: !startloa, !loa_start, !loastart) – pause your baseline fees, housing rent and Trauma Team while away.\n"
                "`!end_loa` (aliases: !endloa, !loa_end, !loaend) – resume all costs when you return. Fixers can specify a member for both commands."
            ),
            inline=False,
        )
        embed.add_field(
            name="🚑 Medical",
            value=(
                "`!call_trauma` – ping the Trauma Team channel with your plan role.\n"
                "`!paycyberware [-v]` – pay your cyberware meds manually."
            ),
            inline=False,
        )


        embed.add_field(
            name="🔫 Gun Stores & Wholesaler",
            value=(
                "`!wh_list` – view current wholesaler lots.\n"
                "`!store_inv` – view your store inventory.\n"
                "`!wh_buy <lot_id> <qty>` – store owners buy lots from corp wholesaler.\n"
                '`!wh_sell @buyer "character_name" <lot_id> <qty> <price>` – complete a gun sale with a receipt posted to audit.\n\n'
                "Run `!helpbusiness` for a full step-by-step guide."
            ),
            inline=False,
        )

        embed.add_field(
            name="📑 Character Sheets",
            value=(
                "`!search_characters <keyword> [-depth N]` – search thread titles, tags and posts with fuzzy matching (Fixers only).\n"
                "`!retire` – move threads tagged 'Retired' to the archive (Fixers only).\n"
                "`!move_npcs` – move threads tagged 'NPC' to the NPC forum (Fixers only).\n"
                "`!unretire <thread_id>` – move a retired thread back (Fixers only).\n"
                "`!export_threads #channel` – export all threads from a channel into a downloadable HTML file (Fixers only)."
            ),
            inline=False,
        )

        embed.set_footer(text="Use !roll, pay your rent, stay alive.")
        await ctx.send(embed=embed)

    @commands.command(name="helpfixer")
    async def helpfixer(self, ctx):
        """Display help for fixers."""

        def embed_len(e: discord.Embed) -> int:
            total = len(e.title or "") + len(e.description or "")
            if e.footer and e.footer.text:
                total += len(e.footer.text)
            for f in e.fields:
                total += len(f.name) + len(str(f.value))
            return total

        fields = [
            (
                "✉️ Messaging Tools",
                "\n".join([
                    "`!dm @user <text>` – send an anonymous DM with optional attachments. The conversation is logged in a private thread. Use `!roll` within that thread to relay dice results.",
                    "`!post <channel|thread> <message>` – send a message or execute a command in another location.",
                    "`!npc_button` – send the NPC role assignment button in the current channel.",
                ]),
            ),
            (
                "📑 RP Management",
                "\n".join([
                    "`!start_rp @users...` (aliases: !startrp, !rp_start, !rpstart) – create a locked RP channel for the listed users and ping Fixers.",
                    "`!end_rp` (aliases: !endrp, !rp_end, !rpend) – archive the current RP channel to the log forum and then delete it.",
                    "`!export_threads #channel` – export all threads from a channel into a downloadable HTML file.",
                ]),
            ),
            (
                "💵 Economy & Rent",
                "\n".join([
                    "`!open_shop` (aliases: !openshop, !os) – record a business opening on Sunday and grant passive income immediately.",
                    "`!attend` – log weekly attendance for a $250 payout.",
                    "`!event_start` (aliases: !eventstart, !open_event, !start_event) – allow !attend and !open_shop for 4 hours outside Sunday when run in #attendance.",
                    "`!due` – display a detailed breakdown of what a user owes on the 1st.",
                    "`!paydue [-v]` – pay your monthly obligations early.",
                    "`!collect_rent [@user] [-v] [-force]` (alias: !collectrent) – run the monthly rent cycle. Use `-force` to ignore the 30\u202fday limit.",
                    "`!collect_housing @user [-v] [-force]` / `!collect_business @user [-v] [-force]` / `!collect_trauma @user [-v] [-force]` – charge specific fees with optional verbose logs. (aliases: !collecthousing / !collectbusiness / !collecttrauma)",
                    "`!list_deficits` – list members who can't cover upcoming charges.",
                ]),
            ),
            (
                "🔫 Wholesaler / Store Tools",
                "\n".join([
                    "`!wh_list` – view current wholesaler lots grouped by weapon type.",
                    "`!store_inv [shop_name]` – view your store inventory (admins can inspect a mapped shop alias).",
                    '`!wh_buy <lot_id> <qty>` – buy stock from the wholesaler into your store.',
                    '`!wh_sell @buyer "character_name" <lot_id> <qty> <price>` – sell to a player (debit buyer, credit seller, post receipt). Alias: `!sell`.',
                    "`!wh_setshop <shop_name> @owner` – bind a shop alias to a specific owner account.",
                    "`!wh_shops` – list all shop alias mappings.",
                    "`!wh_restock [seed]` – regenerate weekly wholesaler stock from the configured sheet source.",
                    "`!wh_clear_inventory` – clear current wholesaler lots without touching store inventories.",
                    "`!wh_recheck` – compare current lots to source sheet values and report mismatches.",
                    "`!wh_gunlist` (aliases: !wh_guns, !wh_masterlist) – list every gun parsed from the master sheet with type, tier and price.",
                    "`!wh_setsheet <xlsx_export_url|off>` – set or clear the runtime master gun list source URL.",
                    "`!wh_restock_settings [key] [value]` – view or tune weekly wholesaler refresh settings (lot counts and qty ranges).",
                    "`!wh_add <gun> <L|M|H> <unit_cost> <qty> [restriction]` / `!store_add @owner <gun> <L|M|H> <unit_cost> <qty> [restriction]` – manually add stock. Restriction: `basic` (default), `controlled`, or `restricted`.",
                    "`!wh_remove <lot_id> [qty]` – remove a lot (or reduce its quantity) from the wholesaler.",
                    "`!store_remove @owner <lot_id> [qty]` – remove a lot (or reduce its quantity) from a store.",
                    "`!wh_approve @user` – add a user to your controlled-buyer list.",
                    "`!wh_unapprove @user` – remove a user from your controlled-buyer list.",
                    "`!wh_approved` – view your controlled-buyer list.",
                    "`!wh_tx <tx_id>` – inspect a transaction by ID.",
                    "`!wh_retry_payout <tx_id>` – retry a pending seller payout.",
                    "`!wh_paths` – show wholesaler data file paths.",
                ]),
            ),
            (
                "🏖️ LOA & Cyberware",
                "\n".join([
                    "`!start_loa [@user]` (aliases: !startloa, !loa_start, !loastart) / `!end_loa [@user]` (aliases: !endloa, !loa_end, !loaend) – toggle LOA for yourself or the specified member.",
                    "`!checkup @user` (aliases: !check-up, !check_up, !cu, !cup) – remove the checkup role once an in-character exam is completed.",
                    "`!weeks_without_checkup @user` (aliases: !wwocup, !wwc) – show how many weeks a member has kept the role without a checkup.",
                    "`!give_checkup_role [@user]` (aliases: !givecheckuprole, !cuall) – give the check-up role to a member or all cyberware users.",
                    "`!checkup_report` (aliases: !cu_report, !cur) – list who did a checkup, who paid meds, and who couldn't pay.",
                    "`!cyberware_status` (aliases: !cstatus, !cstat) – show current week status for all cyberware users.",
                    "`!collect_cyberware @user [-v]` – manually charge a member for their meds and show the last few log lines unless `-v` is supplied.",
                    "`!paycyberware [-v]` – pay your own cyberware meds manually.",
                ]),
            ),
        ]

        embeds = []
        current = discord.Embed(
            title="🛠️ NCRP Bot — Fixer Help",
            description="Advanced commands for messaging, RP management, and rent.",
            color=discord.Color.purple(),
        )
        for name, value in fields:
            chunks = [value[i : i + 1024] for i in range(0, len(value), 1024)] or [""]
            for i, chunk in enumerate(chunks):
                field_name = name if i == 0 else "\u200b"
                if embed_len(current) + len(field_name) + len(chunk) > 5800:
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

        def embed_len(e: discord.Embed) -> int:
            total = len(e.title or "") + len(e.description or "")
            if e.footer and e.footer.text:
                total += len(e.footer.text)
            for f in e.fields:
                total += len(f.name) + len(str(f.value))
            return total

        fields = [
            (
                "⚙️ System Control",
                "\n".join([
                    "`!enable_system <name>` / `!disable_system <name>` (aliases: !es/!ds) – toggle major subsystems.",
                    "`!system_status` – display the current enable/disable flags.",
                ]),
            ),
            (
                "🛠️ Admin Tools",
                "\n".join([
                    "`!test_bot [tests] [-silent] [-verbose]` – execute the built-in test suite. Results can be DMed when `-silent` is used and step details are shown with `-verbose`. Prefixes run groups of tests.",
                    "`!list_tests` – show all available self-test names.",
                    "`!test__bot [pattern]` – run the PyTest suite optionally filtering by pattern.",
                    "`!shutdown_bot` (aliases: !shutdownbot, !forceshutdown) – log an audit message and cleanly shut down the bot process.",
                    "`!backfill_logs [limit]` – rebuild attendance and business open logs from recent message history.",
                    "`!reindex_tickets [limit]` (alias: !reindextickets) – scan the bot-logs channel and build a local ticket search index. Run once to seed history; new tickets index automatically.",
                    "`!search_tickets <query>` (aliases: !searchtickets, !ticketsearch) – instantly search the local ticket index by name, user, ticket ID, reason, or any text.",
                ]),
            ),
            (
                "💵 Simulations & Backups",
                "\n".join([
                    "`!simulate_rent [@user] [-v]` (alias: !simulaterent) – perform a dry run of rent collection using the same options.",
                    "`!simulate_cyberware [@user] [week]` – preview cyberware medication costs globally or for a certain week.",
                    "`!simulate_all [@user]` – run both simulations at once.",
                    "`!backup_balances` – save all member balances to a timestamped file.",
                    "`!backup_balance @user` – save one member's balance to a file.",
                    "`!restore_balances <file>` – restore balances from a backup file.",
                    "`!restore_balance @user [file]` – restore one member's balance from a backup.",
                ]),
            ),
        ]

        embeds = []
        current = discord.Embed(
            title="🛠️ NCRP Bot — Admin Help",
            description="Commands for admins only.",
            color=discord.Color.dark_gold(),
        )
        for name, value in fields:
            chunks = [value[i : i + 1024] for i in range(0, len(value), 1024)] or [""]
            for i, chunk in enumerate(chunks):
                field_name = name if i == 0 else "\u200b"
                if embed_len(current) + len(field_name) + len(chunk) > 5800:
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

    @commands.command(name="helpbusiness", aliases=["helpshop", "helpstore"])
    async def helpbusiness(self, ctx):
        """Display help for gun store business owners."""
        embed = discord.Embed(
            title="🔫 NCRP Bot — Gun Store Owner Help",
            description=(
                "Everything you need to run your gun store. "
                "You buy stock from the corporate wholesaler, then sell to players at your own markup."
            ),
            color=discord.Color.orange(),
        )

        embed.add_field(
            name="📋 Step 1 — Check Wholesaler Stock",
            value=(
                "`!wh_list` — see what the wholesaler currently has available.\n"
                "Lots are grouped by weapon type (Pistol, Revolver, Shotgun, etc.).\n"
                "Each lot shows: **Lot ID**, gun name, tier (L/M/H), cost per unit, and quantity."
            ),
            inline=False,
        )

        embed.add_field(
            name="🛒 Step 2 — Buy Stock for Your Store",
            value=(
                "`!wh_buy <lot_id> <qty>`\n"
                "Example: `!wh_buy lot-a3f2 5` — buys 5 units from that lot.\n\n"
                "The total cost (unit price x qty) is deducted from your balance.\n"
                "The guns move into your store inventory automatically."
            ),
            inline=False,
        )

        embed.add_field(
            name="📦 Step 3 — Check Your Store Inventory",
            value=(
                "`!store_inv` — view what you currently have in stock.\n"
                "Each item shows a **Lot ID** you'll use when selling to players."
            ),
            inline=False,
        )

        embed.add_field(
            name="💰 Step 4 — Sell to a Player",
            value=(
                '`!wh_sell @buyer "character_name" <lot_id> <qty> <price>`\n'
                'Example: `!wh_sell @Johnny "V" lot-a3f2 1 2500`\n\n'
                "This will:\n"
                "1. Deduct the price from the buyer's balance\n"
                "2. Credit the price to your balance\n"
                "3. Remove the item(s) from your inventory\n"
                "4. Post an audit receipt for staff records\n\n"
                "You can also use `!sell` as a shortcut."
            ),
            inline=False,
        )

        embed.add_field(
            name="🔒 Restrictions",
            value=(
                "Guns have a restriction level: **basic**, **controlled**, or **restricted**.\n"
                "- **Basic** — anyone can buy (default).\n"
                "- **Controlled** — only buyers on your approved list can purchase.\n"
                "- **Restricted** — approved list + an admin must approve each sale.\n\n"
                "`!wh_approve @user` — add someone to your approved buyer list.\n"
                "`!wh_unapprove @user` — remove someone.\n"
                "`!wh_approved` — view your list."
            ),
            inline=False,
        )

        embed.add_field(
            name="🏪 Other Useful Commands",
            value=(
                "`!wh_shops` — see all registered shop aliases and owners."
            ),
            inline=False,
        )

        embed.add_field(
            name="⚠️ Good to Know",
            value=(
                "- Wholesaler stock refreshes **weekly** — buy what you need before it's gone.\n"
                "- You set your own sale prices — the wholesaler cost is your floor.\n"
                "- If a sale's payout to you fails, staff can retry it with `!wh_retry_payout`.\n"
                "- Your store inventory is separate from the wholesaler — clearing wholesaler stock won't touch your shelves."
            ),
            inline=False,
        )

        embed.set_footer(text="Buy low, sell high, stay strapped. | Use !helpme for general help.")
        await ctx.send(embed=embed)

    @commands.command(name="shutdown_bot", aliases=["shutdownbot", "forceshutdown"])
    @commands.has_permissions(administrator=True)
    async def shutdown_bot(self, ctx):
        """Force a clean bot shutdown with audit logging."""
        await ctx.send("🛑 Shutdown requested. Closing bot process...")
        wholesaler = self.bot.get_cog("WholesalerCog")
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

        attend_data = await load_json_file(config.ATTEND_LOG_FILE, default={})
        open_data = await load_json_file(config.OPEN_LOG_FILE, default={})

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
                        entries = attend_data.setdefault(uid, [])
                        if ts not in entries:
                            entries.append(ts)
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
                        entries = open_data.setdefault(uid, [])
                        if ts not in entries:
                            entries.append(ts)
                            open_added += 1

        await save_json_file(config.ATTEND_LOG_FILE, attend_data)
        await save_json_file(config.OPEN_LOG_FILE, open_data)

        await ctx.send(
            f"✅ Backfilled {attend_added} attendance entries and {open_added} business opens."
        )
        await self.log_audit(
            ctx.author,
            f"Backfilled logs: attend {attend_added}, open {open_added}",
        )

    @commands.Cog.listener()
    async def on_command_error(self, ctx, error):
        """Global error handler for commands."""
        # Ignore errors triggered by other bots to avoid feedback loops
        if getattr(ctx.author, "bot", False):
            return
        if isinstance(error, commands.CommandNotFound):
            # Ignore specific economy bot commands entirely
            cmd = ctx.message.content.lstrip(self.bot.command_prefix).split()[0].lower()
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
        else:
            await ctx.send(f"⚠️ Error: {str(error)}")
            await self.log_audit(
                ctx.author, f"⚠️ Error: {ctx.message.content} → {str(error)}"
            )

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

        channel = ctx.guild.get_channel(channel_id)
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
                    pass

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
    async def reindex_tickets(self, ctx, limit: int = 10000):
        """Scan the Tickety log channel and rebuild the local search index.

        Run this once to seed the index from history. New tickets are indexed
        automatically going forward. Pass a limit to cap how many messages to scan.
        """
        channel = ctx.guild.get_channel(config.TICKETY_LOG_CHANNEL_ID)
        if channel is None:
            await ctx.send("⚠️ TICKETY_LOG_CHANNEL_ID is not set or channel not found.")
            return

        status_msg = await ctx.send(
            f"⏳ Reindexing `#{channel.name}` (up to {limit:,} messages) — "
            f"this runs in the background, I'll report when done."
        )
        added = 0
        scanned = 0
        # Fetch newest-first so most recent tickets land in the index first.
        # Sleep 1 s every 100 messages (= every API call) to stay well under rate limits.
        async for message in channel.history(limit=limit, oldest_first=False):
            scanned += 1
            if await self._index_message(message, save=False):
                added += 1
            if scanned % 100 == 0:
                await asyncio.sleep(1)

        await self._save_ticket_index()

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

        shown = matches[:25]
        embed = discord.Embed(
            title=f"🎫 Ticket Search: {query}",
            description=(
                f"Found **{len(matches)}** match{'es' if len(matches) != 1 else ''}  "
                f"({len(self._ticket_index):,} tickets indexed)"
                + (f" — showing newest 25" if len(matches) > 25 else "")
            ),
            color=discord.Color.blurple(),
        )
        lines = []
        for e in shown:
            ts_str = e["ts"][:10]  # YYYY-MM-DD
            title = e["title"][:60]
            lines.append(f"[{ts_str}]({e['url']}) — {title}")

        chunk = ""
        for line in lines:
            if len(chunk) + len(line) + 1 > 1024:
                embed.add_field(name="Matches", value=chunk, inline=False)
                chunk = line + "\n"
            else:
                chunk += line + "\n"
        if chunk:
            embed.add_field(name="Matches", value=chunk, inline=False)

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
