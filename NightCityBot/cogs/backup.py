"""Backup cog — admin-only commands for Google Drive database backups."""

import json
import logging
import os
import traceback
from datetime import datetime, time as dtime, timezone

import discord
from discord.ext import commands, tasks

import config
from NightCityBot.utils.permissions import is_fixer
from NightCityBot.utils.db_backup import (
    export_all_tables,
    decompress_export,
    import_all_tables,
    collect_local_backup_files,
)
from NightCityBot.utils.gdrive_backup import (
    upload_bytes,
    list_backups,
    download_backup,
    rotate_old_backups,
    get_last_backup,
)
from NightCityBot.utils import db as _db

logger = logging.getLogger(__name__)

BACKUP_HOUR = int(os.getenv("BACKUP_HOUR", "4"))
BACKUP_MINUTE = int(os.getenv("BACKUP_MINUTE", "0"))


class Backup(commands.Cog):
    """Admin-only backup and restore commands with automated daily backups."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._last_backup_time: datetime | None = None
        self._last_backup_file: str | None = None
        self._last_backup_size: int | None = None
        self._last_backup_link: str | None = None

    async def cog_load(self) -> None:
        self.daily_backup_loop.start()

    async def cog_unload(self) -> None:
        self.daily_backup_loop.cancel()

    async def _audit_log(self, message: str) -> None:
        try:
            ch_id = getattr(config, "AUDIT_LOG_CHANNEL_ID", 0)
            channel = self.bot.get_channel(ch_id)
            if channel:
                await channel.send(message)
        except Exception:
            logger.warning("Could not post to audit channel", exc_info=True)

    async def _run_backup(self) -> dict:
        pool = await _db.get_pool()
        export_data = await export_all_tables(pool)

        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        filename = f"nightcitybot_backup_{ts}.json.gz"

        local_files = collect_local_backup_files()
        bundle: dict = {
            "db_export": export_data,
            "local_files": {},
        }
        for lf in local_files:
            try:
                with open(lf["path"], "r", encoding="utf-8") as f:
                    content = f.read()
                key = f"{lf['label']}/{lf['name']}"
                bundle["local_files"][key] = content
            except Exception:
                logger.warning("Could not read local file %s", lf["path"], exc_info=True)

        bundle_json = json.dumps(bundle, default=str, ensure_ascii=False).encode("utf-8")
        import gzip
        bundle_compressed = gzip.compress(bundle_json)

        result = upload_bytes(bundle_compressed, filename)

        rotate_old_backups()

        self._last_backup_time = datetime.now(timezone.utc)
        self._last_backup_file = filename
        self._last_backup_size = len(bundle_compressed)
        self._last_backup_link = result.get("webViewLink", "")

        table_meta = export_data.get("metadata", {}).get("tables", [])
        total_rows = sum(t.get("row_count", 0) for t in table_meta)

        return {
            "filename": filename,
            "size": len(bundle_compressed),
            "tables": len(table_meta),
            "total_rows": total_rows,
            "local_files": len(local_files),
            "drive_id": result.get("id", ""),
            "drive_link": result.get("webViewLink", ""),
        }

    @commands.command(name="backup_now")
    @is_fixer()
    async def backup_now(self, ctx: commands.Context) -> None:
        """Trigger an immediate full database backup to Google Drive."""
        msg = await ctx.send("⏳ Starting backup…")
        try:
            info = await self._run_backup()
            size_kb = info["size"] / 1024
            embed = discord.Embed(
                title="✅ Backup Complete",
                color=discord.Color.green(),
            )
            embed.add_field(name="File", value=info["filename"], inline=False)
            embed.add_field(name="Size", value=f"{size_kb:.1f} KB", inline=True)
            embed.add_field(name="Tables", value=str(info["tables"]), inline=True)
            embed.add_field(name="Rows", value=str(info["total_rows"]), inline=True)
            embed.add_field(
                name="Local Files Bundled",
                value=str(info["local_files"]),
                inline=True,
            )
            if info["drive_link"]:
                embed.add_field(
                    name="Drive Link", value=info["drive_link"], inline=False
                )
            await msg.edit(content=None, embed=embed)
            await self._audit_log(
                f"💾 **Backup completed** by {ctx.author.mention}: "
                f"{info['filename']} ({size_kb:.1f} KB, {info['tables']} tables, "
                f"{info['total_rows']} rows)"
            )
        except Exception as e:
            tb = traceback.format_exc()
            await msg.edit(content=f"❌ Backup failed: {e}")
            await self._audit_log(f"🔴 **Backup failed** (manual by {ctx.author.mention}): {e}")
            logger.error("Backup failed:\n%s", tb)

    @commands.command(name="backup_status")
    @is_fixer()
    async def backup_status(self, ctx: commands.Context) -> None:
        """Show the last successful backup time, file size, and Drive link."""
        embed = discord.Embed(
            title="📊 Backup Status", color=discord.Color.blue()
        )

        if self._last_backup_time:
            embed.add_field(
                name="Last Backup",
                value=f"<t:{int(self._last_backup_time.timestamp())}:R>",
                inline=True,
            )
            if self._last_backup_file:
                embed.add_field(name="File", value=self._last_backup_file, inline=True)
            if self._last_backup_size:
                embed.add_field(
                    name="Size",
                    value=f"{self._last_backup_size / 1024:.1f} KB",
                    inline=True,
                )
            if self._last_backup_link:
                embed.add_field(
                    name="Drive Link", value=self._last_backup_link, inline=False
                )
        else:
            embed.description = "No backups have been made during this session."

        try:
            last_on_drive = get_last_backup()
            if last_on_drive:
                embed.add_field(
                    name="Latest on Drive",
                    value=(
                        f"**{last_on_drive['name']}** "
                        f"({int(last_on_drive.get('size', 0)) / 1024:.1f} KB)\n"
                        f"Created: {last_on_drive.get('createdTime', 'unknown')}"
                    ),
                    inline=False,
                )
        except Exception:
            embed.set_footer(text="Could not fetch Drive status — credentials may not be configured.")

        await ctx.send(embed=embed)

    @commands.command(name="restore_db")
    @is_fixer()
    async def restore_db(self, ctx: commands.Context, backup_id: str = "") -> None:
        """List available backups or restore from a specific one.

        Usage:
            !restore_db          — list available backups
            !restore_db <id>     — restore from a specific backup (requires confirmation)
        """
        if not backup_id:
            try:
                backups = list_backups(limit=10)
            except Exception as e:
                await ctx.send(f"❌ Could not list backups: {e}")
                return

            if not backups:
                await ctx.send("No backups found on Google Drive.")
                return

            embed = discord.Embed(
                title="📋 Available Backups",
                description="Use `!restore_db <id>` to restore from a specific backup.",
                color=discord.Color.orange(),
            )
            for b in backups:
                size_kb = int(b.get("size", 0)) / 1024
                embed.add_field(
                    name=b["name"],
                    value=(
                        f"ID: `{b['id']}`\n"
                        f"Size: {size_kb:.1f} KB\n"
                        f"Created: {b.get('createdTime', 'unknown')}"
                    ),
                    inline=False,
                )
            await ctx.send(embed=embed)
            return

        await ctx.send(
            f"⚠️ **WARNING**: This will **overwrite ALL current database data** "
            f"with backup `{backup_id}`.\n\n"
            f"Type `CONFIRM` within 30 seconds to proceed, or anything else to cancel."
        )

        def check(m: discord.Message) -> bool:
            return m.author == ctx.author and m.channel == ctx.channel

        try:
            reply = await self.bot.wait_for("message", check=check, timeout=30.0)
        except Exception:
            await ctx.send("⏰ Restore cancelled — timed out.")
            return

        if reply.content.strip() != "CONFIRM":
            await ctx.send("❌ Restore cancelled.")
            return

        msg = await ctx.send("⏳ Downloading and restoring backup…")
        try:
            data = download_backup(backup_id)
            export_data = decompress_export(data)

            if "db_export" in export_data:
                db_data = export_data["db_export"]
            else:
                db_data = export_data

            pool = await _db.get_pool()
            imported = await import_all_tables(pool, db_data)

            total_rows = sum(imported.values())
            embed = discord.Embed(
                title="✅ Restore Complete",
                color=discord.Color.green(),
            )
            embed.add_field(name="Tables", value=str(len(imported)), inline=True)
            embed.add_field(name="Total Rows", value=str(total_rows), inline=True)
            await msg.edit(content=None, embed=embed)
            await self._audit_log(
                f"🔄 **Database restored** by {ctx.author.mention} "
                f"from backup `{backup_id}` ({len(imported)} tables, {total_rows} rows)"
            )
        except Exception as e:
            tb = traceback.format_exc()
            await msg.edit(content=f"❌ Restore failed: {e}")
            await self._audit_log(
                f"🔴 **Restore failed** by {ctx.author.mention}: {e}"
            )
            logger.error("Restore failed:\n%s", tb)

    @tasks.loop(
        time=dtime(hour=BACKUP_HOUR, minute=BACKUP_MINUTE, tzinfo=timezone.utc)
    )
    async def daily_backup_loop(self) -> None:
        try:
            info = await self._run_backup()
            size_kb = info["size"] / 1024
            await self._audit_log(
                f"💾 **Automated daily backup completed**: "
                f"{info['filename']} ({size_kb:.1f} KB, {info['tables']} tables, "
                f"{info['total_rows']} rows, {info['local_files']} local files)"
            )
        except Exception as e:
            await self._audit_log(f"🔴 **Automated daily backup failed**: {e}")
            logger.error("Automated daily backup failed", exc_info=True)

    @daily_backup_loop.before_loop
    async def _before_daily_backup(self) -> None:
        await self.bot.wait_until_ready()
