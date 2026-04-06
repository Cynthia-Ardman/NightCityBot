"""Error isolation helpers for Discord UI interactions (Views).

Provides on_error implementations that log errors and send ephemeral
feedback so one user's failure never crashes the View for other users.
Also provides auto-deleting ephemeral message helpers.
"""
import asyncio
import logging
from typing import Optional, Union

import discord

logger = logging.getLogger(__name__)

_USER_ERROR_MSG = "\u26a0\ufe0f Something went wrong. Please try again in a moment."

EPHEMERAL_DELETE_DELAY = 300

_ephemeral_delete_tasks: set = set()


def schedule_ephemeral_delete(
    target: Union[discord.Message, discord.WebhookMessage, discord.Interaction],
    *,
    delay: int = EPHEMERAL_DELETE_DELAY,
) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    async def _delete() -> None:
        await asyncio.sleep(delay)
        try:
            if isinstance(target, discord.Interaction):
                await target.delete_original_response()
            elif hasattr(target, "delete"):
                await target.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass
        except Exception:
            logger.debug("ephemeral auto-delete failed", exc_info=True)

    task = loop.create_task(_delete())
    _ephemeral_delete_tasks.add(task)
    task.add_done_callback(_ephemeral_delete_tasks.discard)


async def send_ephemeral(
    interaction: discord.Interaction,
    content: str = "",
    **kwargs,
) -> Optional[discord.WebhookMessage]:
    kwargs.pop("ephemeral", None)
    msg = await interaction.followup.send(content, ephemeral=True, **kwargs)
    if msg is not None:
        schedule_ephemeral_delete(msg)
    return msg


async def respond_ephemeral(
    interaction: discord.Interaction,
    content: str = "",
    **kwargs,
) -> None:
    kwargs.pop("ephemeral", None)
    await interaction.response.send_message(content, ephemeral=True, **kwargs)
    schedule_ephemeral_delete(interaction)


async def _safe_respond(interaction: discord.Interaction, msg: str) -> None:
    try:
        if interaction.response.is_done():
            m = await interaction.followup.send(msg, ephemeral=True)
            if m is not None:
                schedule_ephemeral_delete(m)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
            schedule_ephemeral_delete(interaction)
    except discord.NotFound:
        logger.debug("Interaction token expired for user %s — cannot send response.", interaction.user.id)
    except Exception:
        pass


async def safe_followup(interaction: discord.Interaction, content: str = "", **kwargs) -> bool:
    """Send a followup, returning False if the interaction token has expired."""
    try:
        if interaction.response.is_done():
            m = await interaction.followup.send(content, **kwargs)
            if kwargs.get("ephemeral") and m is not None:
                schedule_ephemeral_delete(m)
        else:
            await interaction.response.send_message(content, **kwargs)
            if kwargs.get("ephemeral"):
                schedule_ephemeral_delete(interaction)
        return True
    except discord.NotFound:
        logger.debug("Interaction token expired for user %s — followup dropped.", interaction.user.id)
        return False
    except discord.HTTPException as e:
        logger.warning("safe_followup HTTP error for user %s: %s", interaction.user.id, e)
        return False


async def view_on_error(
    view: discord.ui.View,
    interaction: discord.Interaction,
    error: Exception,
    item: discord.ui.Item,
) -> None:
    logger.error(
        "View %s interaction error (user=%s, item=%s): %s",
        type(view).__name__,
        interaction.user.id,
        getattr(item, "label", type(item).__name__),
        error,
        exc_info=True,
    )
    await _safe_respond(interaction, _USER_ERROR_MSG)


async def log_panel_failure(
    bot,
    channel_id_attr: str,
    action: str,
    user: discord.User,
    reason: str,
) -> None:
    try:
        ch_id = getattr(bot, channel_id_attr, None) or int(
            getattr(__import__("config"), channel_id_attr, 0)
        )
        if not ch_id:
            return
        ch = bot.get_channel(ch_id)
        if ch is None:
            try:
                ch = await bot.fetch_channel(ch_id)
            except Exception:
                return
        await ch.send(
            f"⚠️ **Panel Failure** — {user.display_name} ({user.id}) "
            f"tried **{action}** → {reason}"
        )
    except Exception:
        logger.debug("log_panel_failure failed", exc_info=True)


class SafeView(discord.ui.View):
    async def on_error(self, interaction, error, item):
        await view_on_error(self, interaction, error, item)

    async def on_timeout(self) -> None:
        msg = getattr(self, "message", None)
        if msg is None:
            return
        try:
            await msg.delete()
        except discord.NotFound:
            pass
        except discord.HTTPException:
            try:
                await msg.edit(content="⏰ This interaction has timed out.", view=None)
            except Exception:
                pass
