import asyncio
import logging
from typing import Dict, Optional

import aiohttp
import config

logger = logging.getLogger(__name__)


class UnbelievaBoatAPI:
    """Minimal async wrapper for the UnbelievaBoat REST API."""

    def __init__(
        self, api_token: str, session: Optional[aiohttp.ClientSession] = None
    ) -> None:
        """Create a new API wrapper."""
        self.api_token = api_token
        self.base_url = f"https://unbelievaboat.com/api/v1/guilds/{config.GUILD_ID}"
        self.headers = {"Authorization": api_token, "Content-Type": "application/json"}
        self.session = session or aiohttp.ClientSession()

    async def close(self) -> None:
        await self.session.close()

    @staticmethod
    def _parse_retry_after(data: dict, attempt: int) -> float:
        try:
            raw = float(data.get("retry_after", 1))
        except (TypeError, ValueError):
            raw = 1.0
        if raw > 1000:
            raw /= 1000
        delay = min(max(raw, 0.25), 30.0)
        return delay

    async def get_balance(self, user_id: int) -> Optional[Dict]:
        """Get a user's balance from UnbelievaBoat."""
        url = f"{self.base_url}/users/{user_id}"
        max_attempts = 5
        for attempt in range(max_attempts):
            try:
                async with self.session.get(url, headers=self.headers) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    if resp.status == 429:
                        try:
                            data = await resp.json()
                        except Exception:
                            data = {}
                        delay = self._parse_retry_after(data, attempt)
                        logger.info(
                            "UnbelievaBoat 429 on GET user %s (attempt %d/%d), retry after %.1fs",
                            user_id, attempt + 1, max_attempts, delay,
                        )
                        await asyncio.sleep(delay)
                        continue
                    logger.warning(
                        "Balance fetch failed (%s): %s", resp.status, await resp.text()
                    )
            except aiohttp.ClientError as e:
                logger.warning(
                    "Balance request error on attempt %s: %s", attempt + 1, e
                )
            await asyncio.sleep(min(1 * (2 ** attempt), 8))
        return None

    async def update_balance(
        self, user_id: int, amount_dict: Dict, reason: str = "Automated rent/income"
    ) -> bool:
        """Update a user's balance on UnbelievaBoat."""
        url = f"{self.base_url}/users/{user_id}"
        payload = amount_dict.copy()
        payload["reason"] = reason

        max_attempts = 5
        for attempt in range(max_attempts):
            try:
                async with self.session.patch(
                    url, headers=self.headers, json=payload
                ) as resp:
                    if resp.status == 200:
                        return True
                    if resp.status == 429:
                        try:
                            data = await resp.json()
                        except Exception:
                            data = {}
                        delay = self._parse_retry_after(data, attempt)
                        logger.info(
                            "UnbelievaBoat 429 on PATCH user %s (attempt %d/%d), retry after %.1fs",
                            user_id, attempt + 1, max_attempts, delay,
                        )
                        await asyncio.sleep(delay)
                        continue
                    error = await resp.text()
                    logger.warning("PATCH failed (%s): %s", resp.status, error)
            except aiohttp.ClientError as e:
                logger.warning("Balance PATCH error on attempt %s: %s", attempt + 1, e)
            await asyncio.sleep(min(1 * (2 ** attempt), 8))
        return False

    async def verify_balance_ops(self, user_id: int) -> bool:
        """Verify the UnbelievaBoat API is reachable for this user by reading their balance.

        Read-only — does NOT modify production data.
        """
        balance = await self.get_balance(user_id)
        return balance is not None
