import asyncio
import logging
import random
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse, parse_qs
import aiohttp
import discord
from discord.ext import commands
from openpyxl import load_workbook

import config
from NightCityBot.services.unbelievaboat import UnbelievaBoatAPI
from NightCityBot.utils import helpers

logger = logging.getLogger(__name__)


class WholesalerCog(commands.Cog):
    """Two-tier gun supply chain: corp wholesaler -> stores -> players.

    Uses read-only spreadsheet parsing and immutable receipt/audit logs.
    Staff updates Character Gun Tracking manually from receipts.
    """

    LEVEL_SETTINGS = {
        "L": {"weight": 70, "qty_min": 3, "qty_max": 10},
        "M": {"weight": 25, "qty_min": 1, "qty_max": 5},
        "H": {"weight": 5, "qty_min": 1, "qty_max": 2},
    }
    DEFAULT_RESTOCK_SETTINGS = {
        "total_lots": 20,
        "lots_L": 14,
        "lots_M": 5,
        "lots_H": 1,
        "qty_min_L": 3,
        "qty_max_L": 10,
        "qty_min_M": 1,
        "qty_max_M": 5,
        "qty_min_H": 1,
        "qty_max_H": 2,
    }

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.unbelievaboat = UnbelievaBoatAPI(config.UNBELIEVABOAT_API_TOKEN)
        self.data_dir = Path(__file__).resolve().parents[1] / "data" / "wholesaler"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.sheet_cache_path = self.data_dir / "master_sheet_latest.xlsx"
        self.state_file = self.data_dir / "state.json"
        self.tx_file = self.data_dir / "transactions.json"
        self.lock = asyncio.Lock()

    def cog_unload(self):
        self.bot.loop.create_task(self.unbelievaboat.close())

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _to_int(value: Any) -> Optional[int]:
        if value is None:
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        stripped = str(value).replace(",", "").replace("$", "").strip()
        if not stripped:
            return None
        try:
            return int(float(stripped))
        except ValueError:
            return None

    @staticmethod
    def _derive_level(effectiveness_raw: str) -> str:
        text = (effectiveness_raw or "").upper()
        if "(M-H)" in text or "(H)" in text:
            return "H"
        if "(M)" in text:
            return "M"
        if "(L)" in text:
            return "L"
        return "L"

    @staticmethod
    def _derive_category(effectiveness_raw: str) -> Optional[str]:
        text = (effectiveness_raw or "").lower()
        for category in ("power", "tech", "smart"):
            if category in text:
                return category.title()
        return None

    @staticmethod
    def _normalize_shop_name(name: str) -> str:
        cleaned = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
        return cleaned

    @staticmethod
    def parse_master_sheet(xlsx_path: str | Path, sheet_name: str) -> list[dict[str, Any]]:
        wb = load_workbook(filename=xlsx_path, read_only=True, data_only=True)
        if sheet_name not in wb.sheetnames:
            raise ValueError(f"Sheet '{sheet_name}' not found in workbook")

        ws = wb[sheet_name]
        row_iter = ws.iter_rows(values_only=True)
        header_row = next(row_iter, None)
        if not header_row:
            wb.close()
            return []

        header = [str(c).strip() if c is not None else "" for c in header_row]
        header_lookup = {name.lower(): idx for idx, name in enumerate(header)}

        def idx_for(options: list[str], fallback: int) -> int:
            for opt in options:
                found = header_lookup.get(opt.lower())
                if found is not None:
                    return found
            return fallback

        name_idx = idx_for(["Gun Name", "Name", "Weapon"], 0)
        eff_idx = idx_for(["Type/Armor Effectiveness", "Type", "Effectiveness"], 1)
        mag_idx = idx_for(["Mag Size", "Mag"], 2)
        price_idx = idx_for(["Price New", "Price", "Price (New)"], 3)
        cyberware_idx = idx_for(["Cyberware Needed", "Cyberware"], 4)

        parsed: list[dict[str, Any]] = []
        for row in row_iter:
            if not row:
                continue

            gun_name = str(row[name_idx]).strip() if name_idx < len(row) and row[name_idx] is not None else ""
            effectiveness_raw = (
                str(row[eff_idx]).strip() if eff_idx < len(row) and row[eff_idx] is not None else ""
            )
            price_new = WholesalerCog._to_int(row[price_idx] if price_idx < len(row) else None)

            if not gun_name:
                continue
            if effectiveness_raw.lower() == "type":
                continue
            if price_new is None or price_new <= 0:
                continue

            mag_raw = row[mag_idx] if mag_idx < len(row) else None
            mag_size = WholesalerCog._to_int(mag_raw)
            if mag_size is None and mag_raw is not None:
                mag_size = str(mag_raw)

            cyber_raw = row[cyberware_idx] if cyberware_idx < len(row) else ""
            cyberware_needed = WholesalerCog._to_int(cyber_raw)
            if cyberware_needed is None:
                cyberware_needed = "" if cyber_raw is None else str(cyber_raw)

            parsed.append(
                {
                    "gun_name": gun_name,
                    "effectiveness_raw": effectiveness_raw,
                    "mag_size": mag_size,
                    "price_new": price_new,
                    "cyberware_needed": cyberware_needed,
                    "gun_level": WholesalerCog._derive_level(effectiveness_raw),
                    "gun_category": WholesalerCog._derive_category(effectiveness_raw),
                }
            )

        wb.close()
        return parsed

    async def _resolve_sheet_path(self) -> Path:
        """Return local xlsx path, downloading from Google Sheets if configured."""
        state = await self._load_state()
        configured_url = str(state.get("settings", {}).get("master_sheet_url", "")).strip()
        sheet_url = configured_url or getattr(config, "WHOLESALER_GOOGLE_SHEET_XLSX_URL", "").strip()
        if not sheet_url:
            return Path(config.WHOLESALER_XLSX_PATH)

        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(sheet_url) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"Failed to fetch sheet export ({resp.status})")
                payload = await resp.read()
                self.sheet_cache_path.write_bytes(payload)
        return self.sheet_cache_path

    async def _load_state(self) -> dict[str, Any]:
        state = await helpers.load_json_file(
            self.state_file,
            default={
                "wholesale_lots": [],
                "stores": {},
                "transactions": 0,
                "pending_payouts": [],
                "shop_registry": {},
                "settings": {},
            },
        )
        state.setdefault("shop_registry", {})
        state.setdefault("stores", {})
        state.setdefault("wholesale_lots", [])
        state.setdefault("pending_payouts", [])
        state.setdefault("settings", {})
        restock = state["settings"].setdefault("restock", {})
        for key, value in self.DEFAULT_RESTOCK_SETTINGS.items():
            restock.setdefault(key, value)
        return state

    @staticmethod
    def _sanitize_positive_int(value: Any, fallback: int) -> int:
        try:
            v = int(value)
            return v if v > 0 else fallback
        except Exception:
            return fallback

    @staticmethod
    def _sanitize_non_negative_int(value: Any, fallback: int) -> int:
        try:
            v = int(value)
            return v if v >= 0 else fallback
        except Exception:
            return fallback

    def _resolve_restock_settings(self, state: dict[str, Any]) -> dict[str, int]:
        raw = state.get("settings", {}).get("restock", {})
        data = {}
        for key, default in self.DEFAULT_RESTOCK_SETTINGS.items():
            if key in {"lots_L", "lots_M", "lots_H"}:
                data[key] = self._sanitize_non_negative_int(raw.get(key), default)
            else:
                data[key] = self._sanitize_positive_int(raw.get(key), default)

        # Keep ranges valid and ensure at least one lot is generated.
        for lvl in ("L", "M", "H"):
            mn_key = f"qty_min_{lvl}"
            mx_key = f"qty_max_{lvl}"
            if data[mn_key] > data[mx_key]:
                data[mn_key], data[mx_key] = data[mx_key], data[mn_key]

        if data["lots_L"] + data["lots_M"] + data["lots_H"] <= 0:
            data["lots_L"] = 1

        data["total_lots"] = max(1, data["total_lots"])

        return data

    def _generate_restock_lots(
        self,
        guns: list[dict[str, Any]],
        cfg: dict[str, int],
        rng: random.Random,
    ) -> tuple[list[dict[str, Any]], dict[str, int]]:
        by_level = {
            "L": [g for g in guns if g["gun_level"] == "L"],
            "M": [g for g in guns if g["gun_level"] == "M"],
            "H": [g for g in guns if g["gun_level"] == "H"],
        }
        weighted = [
            g
            for g in guns
            for _ in range(self.LEVEL_SETTINGS.get(g["gun_level"], {"weight": 1})["weight"])
        ]

        lots: list[dict[str, Any]] = []
        level_totals = {"L": 0, "M": 0, "H": 0}

        for requested_level in ("L", "M", "H"):
            target = cfg[f"lots_{requested_level}"]
            pool = by_level[requested_level] if by_level[requested_level] else weighted
            if not pool or target <= 0:
                continue

            for _ in range(target):
                gun = rng.choice(pool)
                actual_level = str(gun.get("gun_level", requested_level))
                if actual_level not in {"L", "M", "H"}:
                    actual_level = requested_level
                qty = rng.randint(cfg[f"qty_min_{actual_level}"], cfg[f"qty_max_{actual_level}"])
                lots.append(
                    {
                        "lot_id": f"lot-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{uuid.uuid4().hex[:6]}",
                        "gun_name": gun["gun_name"],
                        "gun_level": actual_level,
                        "unit_cost": int(gun["price_new"]),
                        "qty_available": qty,
                        "created_at": self._now_iso(),
                    }
                )
                level_totals[actual_level] += qty

        if len(lots) > cfg["total_lots"]:
            lots = rng.sample(lots, cfg["total_lots"])
            level_totals = {
                "L": sum(int(lot.get("qty_available", 0)) for lot in lots if lot.get("gun_level") == "L"),
                "M": sum(int(lot.get("qty_available", 0)) for lot in lots if lot.get("gun_level") == "M"),
                "H": sum(int(lot.get("qty_available", 0)) for lot in lots if lot.get("gun_level") == "H"),
            }

        return lots, level_totals

    async def _save_state(self, state: dict[str, Any]) -> bool:
        return await helpers.save_json_file(self.state_file, state)

    async def _append_tx(self, tx: dict[str, Any]) -> bool:
        return await helpers.append_json_file(self.tx_file, tx)

    def _is_admin(self, member: discord.Member) -> bool:
        admin_role_ids = set(getattr(config, "WHOLESALER_ADMIN_ROLE_IDS", []))
        return any(r.id in admin_role_ids for r in member.roles)

    def _is_store_owner(self, member: discord.Member) -> bool:
        store_role_ids = set(getattr(config, "WHOLESALER_STORE_ROLE_IDS", []))
        return any(r.id in store_role_ids for r in member.roles)

    async def _audit_send(self, text: str) -> None:
        channel = self.bot.get_channel(getattr(config, "WHOLESALER_AUDIT_CHANNEL_ID", 0))
        if not channel:
            logger.warning("Missing wholesaler audit channel id=%s", getattr(config, "WHOLESALER_AUDIT_CHANNEL_ID", 0))
            return
        try:
            await channel.send(text)
        except Exception:
            logger.exception("Failed to send wholesaler audit line")

    async def _ensure_member(self, ctx: commands.Context) -> Optional[discord.Member]:
        if not ctx.guild or not isinstance(ctx.author, discord.Member):
            await ctx.send("❌ This command can only be used in the server.")
            return None
        return ctx.author

    @staticmethod
    def _store_id(guild_id: int, owner_id: int) -> str:
        return f"{guild_id}:{owner_id}"

    def _build_tx(self, tx_type: str, **kwargs: Any) -> dict[str, Any]:
        return {
            "tx_id": f"tx-{uuid.uuid4().hex[:12]}",
            "type": tx_type,
            "timestamp": self._now_iso(),
            "status": "SUCCESS",
            "error_details": "",
            **kwargs,
        }

    async def _resolve_store_owner_id(
        self, ctx: commands.Context, state: dict[str, Any], shop_or_mention: Optional[str], default_owner: int
    ) -> int:
        if not shop_or_mention:
            return default_owner
        if shop_or_mention.startswith("<@") and shop_or_mention.endswith(">"):
            return int(shop_or_mention.strip("<@!>"))
        key = self._normalize_shop_name(shop_or_mention)
        return int(state.get("shop_registry", {}).get(key, default_owner))

    async def _get_total_balance(self, user_id: int) -> Optional[tuple[int, int, int]]:
        balance = await self.unbelievaboat.get_balance(user_id)
        if not balance:
            return None
        cash = int(balance.get("cash", 0))
        bank = int(balance.get("bank", 0))
        return cash, bank, cash + bank

    async def _deduct_funds(self, user_id: int, amount: int, reason: str) -> tuple[bool, str]:
        balances = await self._get_total_balance(user_id)
        if balances is None:
            return False, "Unable to fetch user balance"
        cash, _bank, total = balances
        if total < amount:
            return False, f"Insufficient funds (${total}/${amount})"

        cash_deduct = min(max(cash, 0), amount)
        bank_deduct = max(0, amount - cash_deduct)
        payload: dict[str, int] = {}
        if cash_deduct:
            payload["cash"] = -cash_deduct
        if bank_deduct:
            payload["bank"] = -bank_deduct
        ok = await self.unbelievaboat.update_balance(user_id, payload, reason=reason)
        return (ok, "" if ok else "Failed to deduct funds")

    async def _credit_funds(self, user_id: int, amount: int, reason: str) -> bool:
        return await self.unbelievaboat.update_balance(user_id, {"cash": amount}, reason=reason)

    @commands.command(name="wh_setshop")
    async def wh_setshop(self, ctx: commands.Context, shop_name: str, owner: discord.Member):
        """Bind shop aliases (shop1/shop2/shop3 etc.) to an owner Discord account."""
        member = await self._ensure_member(ctx)
        if not member:
            return
        if not self._is_admin(member):
            await ctx.send("❌ Admin role required.")
            return

        normalized = self._normalize_shop_name(shop_name)
        if not normalized:
            await ctx.send("❌ Invalid shop name.")
            return

        async with self.lock:
            state = await self._load_state()
            state.setdefault("shop_registry", {})[normalized] = owner.id
            await self._save_state(state)

        await ctx.send(f"✅ `{normalized}` is now mapped to {owner.mention}.")

    @commands.command(name="wh_shops")
    async def wh_shops(self, ctx: commands.Context):
        member = await self._ensure_member(ctx)
        if not member:
            return
        state = await self._load_state()
        registry = state.get("shop_registry", {})
        if not registry:
            await ctx.send("No shop aliases configured.")
            return
        lines = [f"`{name}` → <@{owner_id}>" for name, owner_id in sorted(registry.items())]
        await ctx.send("**Shop Registry**\n" + "\n".join(lines[:30]))

    @commands.command(name="wh_list")
    async def wh_list(self, ctx: commands.Context):
        state = await self._load_state()
        lots = [lot for lot in state.get("wholesale_lots", []) if int(lot.get("qty_available", 0)) > 0]
        if not lots:
            await ctx.send("No wholesale lots available.")
            return
        lines = [
            f"`{l['lot_id']}` | {l['gun_name']} ({l['gun_level']}) | ${l['unit_cost']} | qty {l['qty_available']}"
            for l in lots[:25]
        ]
        await ctx.send("**Wholesaler Stock**\n" + "\n".join(lines))

    @commands.command(name="store_inv")
    async def store_inv(self, ctx: commands.Context, *, shop: Optional[str] = None):
        """Show your inventory or a named shop inventory (`!store_inv shop1`)."""
        member = await self._ensure_member(ctx)
        if not member:
            return

        state = await self._load_state()
        owner_id = await self._resolve_store_owner_id(ctx, state, shop, member.id)
        if owner_id != member.id and not self._is_admin(member):
            await ctx.send("❌ Only admins can inspect other shops.")
            return

        store_id = self._store_id(ctx.guild.id, owner_id)
        lots = [l for l in state.get("stores", {}).get(store_id, {}).get("lots", []) if l.get("qty_remaining", 0) > 0]
        if not lots:
            await ctx.send("Store inventory is empty.")
            return

        shop_title = shop or f"owner:{owner_id}"
        lines = [
            f"`{l['lot_id']}` | {l['gun_name']} ({l['gun_level']}) | cost ${l['unit_cost']} | qty {l['qty_remaining']}"
            for l in lots[:30]
        ]
        await ctx.send(f"**Store Inventory ({shop_title})**\n" + "\n".join(lines))

    @commands.command(name="wh_buy")
    async def wh_buy(self, ctx: commands.Context, lot_id: str, qty: int):
        member = await self._ensure_member(ctx)
        if not member:
            return
        if not self._is_store_owner(member):
            await ctx.send("❌ Store owner role required.")
            return
        if qty <= 0:
            await ctx.send("❌ qty must be > 0.")
            return

        async with self.lock:
            state = await self._load_state()
            lot = next((l for l in state.get("wholesale_lots", []) if l.get("lot_id") == lot_id), None)
            if not lot or int(lot.get("qty_available", 0)) < qty:
                await ctx.send("❌ Lot unavailable or insufficient quantity.")
                return

            total = int(lot["unit_cost"]) * qty
            ok, err = await self._deduct_funds(member.id, total, f"Wholesale purchase {lot_id}")
            tx = self._build_tx(
                "WHOLESALE_BUY",
                seller_id="WHOLESALER",
                buyer_id=member.id,
                gun_name=lot["gun_name"],
                gun_level=lot["gun_level"],
                qty=qty,
                unit_price=lot["unit_cost"],
                total_price=total,
                lot_id=lot_id,
            )
            if not ok:
                tx["status"] = "FAILED"
                tx["error_details"] = err
                await self._append_tx(tx)
                await ctx.send(f"❌ Purchase failed: {err}")
                return

            lot["qty_available"] -= qty
            store_id = self._store_id(ctx.guild.id, member.id)
            store = state.setdefault("stores", {}).setdefault(store_id, {"owner_id": member.id, "lots": []})
            existing = next((l for l in store["lots"] if l.get("lot_id") == lot_id), None)
            if existing:
                existing["qty_remaining"] += qty
            else:
                store["lots"].append(
                    {
                        "lot_id": lot_id,
                        "gun_name": lot["gun_name"],
                        "gun_level": lot["gun_level"],
                        "unit_cost": lot["unit_cost"],
                        "qty_remaining": qty,
                    }
                )
            await self._save_state(state)
            await self._append_tx(tx)

        await ctx.send(f"✅ Purchased {qty}x {lot['gun_name']} for ${total}.")
        await self._audit_send(
            f"[WHOLESALE_BUY] tx={tx['tx_id']} buyer={member.mention} gun={lot['gun_name']} level={lot['gun_level']} qty={qty} total={total} lot={lot_id}"
        )

    @commands.command(name="sell")
    async def sell(
        self,
        ctx: commands.Context,
        buyer: discord.Member,
        lot_id: str,
        qty: int,
        total_price: int,
        *,
        extra: str = "",
    ):
        member = await self._ensure_member(ctx)
        if not member:
            return
        if not self._is_store_owner(member):
            await ctx.send("❌ Store owner role required.")
            return
        if qty <= 0 or total_price <= 0:
            await ctx.send("❌ qty and total_price must be > 0.")
            return

        character_name = ""
        if "character:" in extra.lower():
            character_name = extra.split(":", 1)[1].strip().strip('"')

        async with self.lock:
            state = await self._load_state()
            store_id = self._store_id(ctx.guild.id, member.id)
            store = state.get("stores", {}).get(store_id)
            if not store:
                await ctx.send("❌ No store inventory found.")
                return

            store_lot = next((l for l in store.get("lots", []) if l.get("lot_id") == lot_id), None)
            if not store_lot or int(store_lot.get("qty_remaining", 0)) < qty:
                await ctx.send("❌ Invalid lot or insufficient quantity.")
                return

            tx = self._build_tx(
                "PLAYER_SALE",
                seller_id=member.id,
                buyer_id=buyer.id,
                gun_name=store_lot["gun_name"],
                gun_level=store_lot["gun_level"],
                qty=qty,
                unit_price=max(1, total_price // qty),
                total_price=total_price,
                lot_id=lot_id,
                character_name=character_name,
            )

            deduct_ok, deduct_err = await self._deduct_funds(
                buyer.id,
                total_price,
                f"Gun purchase from {member.id} ({store_lot['gun_name']})",
            )
            if not deduct_ok:
                tx["status"] = "FAILED"
                tx["error_details"] = deduct_err
                await self._append_tx(tx)
                await ctx.send(f"❌ Sale failed: {deduct_err}")
                return

            payout_ok = await self._credit_funds(
                member.id,
                total_price,
                f"Gun sale to {buyer.id} ({store_lot['gun_name']})",
            )
            if not payout_ok:
                tx["status"] = "PENDING_PAYOUT"
                tx["error_details"] = "Buyer charged, seller payout failed"
                state.setdefault("pending_payouts", []).append(
                    {"tx_id": tx["tx_id"], "seller_id": member.id, "amount": total_price}
                )
                await self._save_state(state)
                await self._append_tx(tx)
                await self._audit_send(
                    f"🚨 [PENDING_PAYOUT] tx={tx['tx_id']} seller={member.mention} buyer={buyer.mention} amount={total_price}"
                )
                await ctx.send("⚠️ Buyer charged, seller payout pending admin retry.")
                return

            store_lot["qty_remaining"] -= qty
            await self._save_state(state)
            await self._append_tx(tx)

        await ctx.send(f"✅ Sold {qty}x {store_lot['gun_name']} for ${total_price}.")
        await self._audit_send(
            "[PLAYER_SALE_RECEIPT] "
            f"tx={tx['tx_id']} ts={tx['timestamp']} seller={member.mention} buyer={buyer.mention} "
            f"character={character_name or 'N/A'} gun={store_lot['gun_name']} level={store_lot['gun_level']} "
            f"qty={qty} total={total_price} unit_cost={store_lot['unit_cost']} lot={lot_id}"
        )

    @commands.command(name="wh_restock")
    async def wh_restock(self, ctx: commands.Context, seed: Optional[int] = None):
        member = await self._ensure_member(ctx)
        if not member:
            return
        if not self._is_admin(member):
            await ctx.send("❌ Admin role required.")
            return

        try:
            sheet_path = await self._resolve_sheet_path()
            guns = self.parse_master_sheet(sheet_path, config.WHOLESALER_MASTER_SHEET_NAME)
        except Exception as e:
            logger.exception("wh_restock failed")
            await ctx.send(f"❌ Restock failed while reading source sheet: {e}")
            return

        if not guns:
            await ctx.send("❌ No valid guns found in source sheet.")
            return

        async with self.lock:
            state = await self._load_state()
            cfg = self._resolve_restock_settings(state)
            rng = random.Random(seed)
            lots, level_totals = self._generate_restock_lots(guns, cfg, rng)

            state["wholesale_lots"] = lots
            state.setdefault("settings", {}).setdefault("restock", {}).update(cfg)
            await self._save_state(state)

        await ctx.send(f"✅ Restocked {len(lots)} wholesale lots.")
        await self._audit_send(
            f"[WHOLESALE_RESTOCK] by={member.mention} lots={len(lots)} qtyL={level_totals['L']} qtyM={level_totals['M']} qtyH={level_totals['H']}"
        )

    async def auto_refresh_weekly_after_cyberware(self) -> bool:
        """Auto-restock once per week, called by cyberware weekly process."""
        try:
            sheet_path = await self._resolve_sheet_path()
            guns = self.parse_master_sheet(sheet_path, config.WHOLESALER_MASTER_SHEET_NAME)
        except Exception:
            logger.exception("Auto wholesaler refresh failed during sheet read")
            return False

        if not guns:
            return False

        async with self.lock:
            state = await self._load_state()
            week_key = datetime.now(timezone.utc).strftime("%Y-W%U")
            last_key = str(state.get("settings", {}).get("last_auto_restock_week", ""))
            if last_key == week_key:
                return True

            cfg = self._resolve_restock_settings(state)
            rng = random.Random()
            lots, _level_totals = self._generate_restock_lots(guns, cfg, rng)

            state["wholesale_lots"] = lots
            state.setdefault("settings", {})["last_auto_restock_week"] = week_key
            await self._save_state(state)

        await self._audit_send(f"[WHOLESALE_AUTO_RESTOCK] lots={len(lots)} week={week_key}")
        return True

    @commands.command(name="wh_restock_settings")
    async def wh_restock_settings(
        self,
        ctx: commands.Context,
        key: Optional[str] = None,
        value: Optional[int] = None,
    ):
        """View or update weekly restock settings.

        Example: !wh_restock_settings lots_L 12
        """
        member = await self._ensure_member(ctx)
        if not member:
            return
        if not self._is_admin(member):
            await ctx.send("❌ Admin role required.")
            return

        async with self.lock:
            state = await self._load_state()
            cfg = self._resolve_restock_settings(state)

            if key and value is not None:
                if key not in self.DEFAULT_RESTOCK_SETTINGS:
                    await ctx.send(
                        "❌ Invalid key. Use one of: "
                        + ", ".join(sorted(self.DEFAULT_RESTOCK_SETTINGS.keys()))
                    )
                    return
                if key in {"lots_L", "lots_M", "lots_H"}:
                    cfg[key] = max(0, int(value))
                else:
                    cfg[key] = max(1, int(value))
                state.setdefault("settings", {}).setdefault("restock", {}).update(cfg)
                await self._save_state(state)
                await ctx.send(f"✅ Updated {key} to {cfg[key]}.")
                return

        lines = ["**Wholesaler Restock Settings**"]
        for k in sorted(self.DEFAULT_RESTOCK_SETTINGS.keys()):
            lines.append(f"`{k}` = {cfg[k]}")
        await ctx.send("\n".join(lines))

    @commands.command(name="wh_setsheet")
    async def wh_setsheet(self, ctx: commands.Context, *, xlsx_export_url: str):
        """Set/clear runtime Google Sheets XLSX export URL for wholesaler source.

        Use `!wh_setsheet off` to clear runtime override and fall back to config.
        """
        member = await self._ensure_member(ctx)
        if not member:
            return
        if not self._is_admin(member):
            await ctx.send("❌ Admin role required.")
            return

        value = xlsx_export_url.strip()
        async with self.lock:
            state = await self._load_state()
            settings = state.setdefault("settings", {})
            if value.lower() in {"off", "none", "clear"}:
                settings.pop("master_sheet_url", None)
                await self._save_state(state)
                await ctx.send("✅ Runtime sheet URL cleared. Using config/default source.")
                return

            if not value.startswith("http"):
                await ctx.send("❌ URL must start with http/https.")
                return

            settings["master_sheet_url"] = value
            await self._save_state(state)

        await ctx.send("✅ Runtime wholesaler sheet URL updated.")
        await self._audit_send(f"[WHOLESALE_SOURCE_SET] by={member.mention} url={value}")

    @commands.command(name="wh_recheck")
    async def wh_recheck(self, ctx: commands.Context):
        """Reconcile current wholesaler lots against current sheet prices/levels."""
        member = await self._ensure_member(ctx)
        if not member:
            return
        if not self._is_admin(member):
            await ctx.send("❌ Admin role required.")
            return

        try:
            sheet_path = await self._resolve_sheet_path()
            guns = self.parse_master_sheet(sheet_path, config.WHOLESALER_MASTER_SHEET_NAME)
        except Exception as e:
            await ctx.send(f"❌ Recheck failed: {e}")
            return

        index = {g["gun_name"].strip().lower(): g for g in guns}
        state = await self._load_state()
        missing = []
        price_mismatch = []
        level_mismatch = []
        for lot in state.get("wholesale_lots", []):
            g = index.get(str(lot.get("gun_name", "")).strip().lower())
            if not g:
                missing.append(lot)
                continue
            if int(g["price_new"]) != int(lot.get("unit_cost", 0)):
                price_mismatch.append((lot, g["price_new"]))
            if g["gun_level"] != lot.get("gun_level"):
                level_mismatch.append((lot, g["gun_level"]))

        lines = [
            f"Checked {len(state.get('wholesale_lots', []))} lots against {len(guns)} sheet rows.",
            f"Missing in sheet: {len(missing)}",
            f"Price mismatches: {len(price_mismatch)}",
            f"Level mismatches: {len(level_mismatch)}",
        ]
        if missing[:3]:
            lines.append("Missing examples: " + ", ".join(m["gun_name"] for m in missing[:3]))
        if price_mismatch[:3]:
            lines.append(
                "Price examples: " + ", ".join(f"{x[0]['gun_name']} lot=${x[0]['unit_cost']} sheet=${x[1]}" for x in price_mismatch[:3])
            )
        if level_mismatch[:3]:
            lines.append(
                "Level examples: " + ", ".join(f"{x[0]['gun_name']} lot={x[0]['gun_level']} sheet={x[1]}" for x in level_mismatch[:3])
            )

        await ctx.send("\n".join(lines))
        await self._audit_send("[WHOLESALE_RECHECK] " + " | ".join(lines[:4]))

    @commands.command(name="wh_add")
    async def wh_add(self, ctx: commands.Context, gun_name: str, level: str, unit_cost: int, qty: int):
        member = await self._ensure_member(ctx)
        if not member:
            return
        if not self._is_admin(member):
            await ctx.send("❌ Admin role required.")
            return
        if unit_cost <= 0 or qty <= 0:
            await ctx.send("❌ unit_cost and qty must be positive.")
            return

        level = level.upper()
        if level not in {"L", "M", "H"}:
            await ctx.send("❌ level must be L/M/H.")
            return

        lot = {
            "lot_id": f"lot-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{uuid.uuid4().hex[:6]}",
            "gun_name": gun_name,
            "gun_level": level,
            "unit_cost": unit_cost,
            "qty_available": qty,
            "created_at": self._now_iso(),
        }
        async with self.lock:
            state = await self._load_state()
            state.setdefault("wholesale_lots", []).append(lot)
            await self._save_state(state)

        tx = self._build_tx(
            "ADMIN_ADJUST",
            seller_id=member.id,
            buyer_id="WHOLESALER",
            gun_name=gun_name,
            gun_level=level,
            qty=qty,
            unit_price=unit_cost,
            total_price=unit_cost * qty,
            lot_id=lot["lot_id"],
        )
        await self._append_tx(tx)
        await ctx.send(f"✅ Added lot `{lot['lot_id']}`.")

    @commands.command(name="store_add")
    async def store_add(
        self,
        ctx: commands.Context,
        store_owner: discord.Member,
        gun_name: str,
        level: str,
        unit_cost: int,
        qty: int,
    ):
        member = await self._ensure_member(ctx)
        if not member:
            return
        if not self._is_admin(member):
            await ctx.send("❌ Admin role required.")
            return
        if unit_cost <= 0 or qty <= 0:
            await ctx.send("❌ unit_cost and qty must be positive.")
            return

        level = level.upper()
        if level not in {"L", "M", "H"}:
            await ctx.send("❌ level must be L/M/H.")
            return

        lot_id = f"lot-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{uuid.uuid4().hex[:6]}"
        store_lot = {
            "lot_id": lot_id,
            "gun_name": gun_name,
            "gun_level": level,
            "unit_cost": unit_cost,
            "qty_remaining": qty,
        }
        async with self.lock:
            state = await self._load_state()
            store_id = self._store_id(ctx.guild.id, store_owner.id)
            store = state.setdefault("stores", {}).setdefault(store_id, {"owner_id": store_owner.id, "lots": []})
            store["lots"].append(store_lot)
            await self._save_state(state)

        tx = self._build_tx(
            "ADMIN_ADJUST",
            seller_id=member.id,
            buyer_id=store_owner.id,
            gun_name=gun_name,
            gun_level=level,
            qty=qty,
            unit_price=unit_cost,
            total_price=unit_cost * qty,
            lot_id=lot_id,
        )
        await self._append_tx(tx)
        await ctx.send(f"✅ Added `{gun_name}` to {store_owner.mention} inventory (lot `{lot_id}`).")

    @commands.command(name="wh_tx")
    async def wh_tx(self, ctx: commands.Context, tx_id: str):
        member = await self._ensure_member(ctx)
        if not member:
            return
        if not self._is_admin(member):
            await ctx.send("❌ Admin role required.")
            return

        txs = await helpers.load_json_file(self.tx_file, default=[])
        tx = next((t for t in txs if t.get("tx_id") == tx_id), None)
        if not tx:
            await ctx.send("Transaction not found.")
            return
        await ctx.send(f"```json\n{tx}\n```")

    @commands.command(name="wh_retry_payout")
    async def wh_retry_payout(self, ctx: commands.Context, tx_id: str):
        member = await self._ensure_member(ctx)
        if not member:
            return
        if not self._is_admin(member):
            await ctx.send("❌ Admin role required.")
            return

        async with self.lock:
            state = await self._load_state()
            pending = state.get("pending_payouts", [])
            entry = next((p for p in pending if p.get("tx_id") == tx_id), None)
            if not entry:
                await ctx.send("No pending payout found.")
                return

            ok = await self._credit_funds(int(entry["seller_id"]), int(entry["amount"]), f"Retry payout {tx_id}")
            if not ok:
                await ctx.send("❌ Retry failed.")
                return

            state["pending_payouts"] = [p for p in pending if p.get("tx_id") != tx_id]
            await self._save_state(state)

        tx = self._build_tx(
            "REFUND",
            seller_id="SYSTEM",
            buyer_id=entry["seller_id"],
            gun_name="PAYOUT_RETRY",
            gun_level="N/A",
            qty=1,
            unit_price=entry["amount"],
            total_price=entry["amount"],
            lot_id=tx_id,
        )
        await self._append_tx(tx)
        await self._audit_send(
            f"[PAYOUT_RETRY_SUCCESS] tx={tx_id} seller=<@{entry['seller_id']}> amount={entry['amount']}"
        )
        await ctx.send("✅ Payout retried.")
