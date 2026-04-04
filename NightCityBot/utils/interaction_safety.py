"""Error isolation helpers for Discord UI interactions (Views).

Provides on_error implementations that log errors and send ephemeral
feedback so one user's failure never crashes the View for other users.
"""
import logging

import discord

logger = logging.getLogger(__name__)

_USER_ERROR_MSG = "\u26a0\ufe0f Something went wrong. Please try again in a moment."


async def _safe_respond(interaction: discord.Interaction, msg: str) -> None:
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except discord.NotFound:
        logger.debug("Interaction token expired for user %s — cannot send response.", interaction.user.id)
    except Exception:
        pass


async def safe_followup(interaction: discord.Interaction, content: str = "", **kwargs) -> bool:
    """Send a followup, returning False if the interaction token has expired."""
    try:
        if interaction.response.is_done():
            await interaction.followup.send(content, **kwargs)
        else:
            await interaction.response.send_message(content, **kwargs)
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
