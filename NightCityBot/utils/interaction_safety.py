"""Error isolation helpers for Discord UI interactions (Views, Modals).

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
    except Exception:
        pass


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


async def modal_on_error(
    modal: discord.ui.Modal,
    interaction: discord.Interaction,
    error: Exception,
) -> None:
    logger.error(
        "Modal %s interaction error (user=%s): %s",
        type(modal).__name__,
        interaction.user.id,
        error,
        exc_info=True,
    )
    await _safe_respond(interaction, _USER_ERROR_MSG)


class SafeView(discord.ui.View):
    async def on_error(self, interaction, error, item):
        await view_on_error(self, interaction, error, item)


class SafeModal(discord.ui.Modal):
    async def on_error(self, interaction, error):
        await modal_on_error(self, interaction, error)
