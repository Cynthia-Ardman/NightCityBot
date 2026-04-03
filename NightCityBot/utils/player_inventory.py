"""Player inventory utility helpers.

Thin façade over the lower-level ``db.py`` functions, exposing the four
canonical verbs the player-inventory system needs:

- ``insert_player_item``   — create one item row
- ``query_player_inventory`` — fetch all items for an owner
- ``delete_player_item``   — remove one item row by UUID
- ``reassign_player_item`` — change the character_name on one item row
"""
from __future__ import annotations

from typing import Optional

from NightCityBot.utils.db import (
    pi_add_item,
    pi_get_by_owner,
    pi_get_item,
    pi_delete_item,
    pi_update_character,
    pi_update_owner,
)


async def insert_player_item(item: dict) -> bool:
    """Insert one item row into ``player_inventory``.

    ``item`` must include at minimum: ``item_id``, ``owner_id``,
    ``character_name``, ``item_type``, ``name``.  Optional fields:
    ``restriction``, ``description``, ``price_paid``, ``seller_id``,
    ``seller_name``, ``acquired_at``.

    Returns ``True`` on success, ``False`` on DB error.
    """
    return await pi_add_item(item)


async def query_player_inventory(owner_id: str) -> list[dict]:
    """Return all ``player_inventory`` rows owned by *owner_id*.

    Returns an empty list on error or when the player has no items.
    """
    return await pi_get_by_owner(owner_id)


async def get_player_item(item_id: str) -> Optional[dict]:
    """Return a single ``player_inventory`` row by UUID, or ``None``."""
    return await pi_get_item(item_id)


async def delete_player_item(item_id: str, *, expected_owner_id: str | None = None) -> bool:
    """Delete the ``player_inventory`` row with the given *item_id*.

    When *expected_owner_id* is provided the DELETE includes an owner guard.

    Returns ``True`` if the row was deleted, ``False`` if not found or on error.
    """
    return await pi_delete_item(item_id, expected_owner_id=expected_owner_id)


async def reassign_player_item(
    item_id: str,
    new_character: str,
    expected_owner_id: str | None = None,
    *,
    new_character_id: str | None = None,
) -> bool:
    """Update the ``character_name`` (and optionally ``character_id``) of one item row.

    Returns ``True`` when exactly one row was updated, ``False`` otherwise.
    """
    return await pi_update_character(item_id, new_character, expected_owner_id, new_character_id=new_character_id)


async def transfer_player_item(
    item_id: str,
    new_owner_id: str,
    new_character: str,
    old_owner_id: str,
    *,
    new_character_id: str | None = None,
) -> bool:
    """Transfer ownership of an item to a new player/character.

    Includes an owner guard (``old_owner_id``) so that stale or concurrent
    commands cannot re-transfer an item that is no longer owned by the sender.

    When *new_character_id* is provided it is also written to the row.

    Returns ``True`` when exactly one row was updated, ``False`` otherwise.
    """
    return await pi_update_owner(item_id, new_owner_id, new_character, old_owner_id, new_character_id=new_character_id)
