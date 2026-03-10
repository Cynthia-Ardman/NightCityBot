import asyncio
from dataclasses import dataclass

from NightCityBot.cogs.wholesaler import WholesalerCog


class FakeUnbelievaBoat:
    def __init__(self, balances: dict[int, dict[str, int]]):
        self.balances = balances
        self.operations: list[tuple[int, dict[str, int], str]] = []

    async def get_balance(self, user_id: int):
        return self.balances.get(user_id, {"cash": 0, "bank": 0})

    async def update_balance(self, user_id: int, payload: dict[str, int], reason: str = ""):
        acct = self.balances.setdefault(user_id, {"cash": 0, "bank": 0})
        next_cash = acct.get("cash", 0) + payload.get("cash", 0)
        next_bank = acct.get("bank", 0) + payload.get("bank", 0)
        if next_cash < 0 or next_bank < 0:
            return False
        acct["cash"] = next_cash
        acct["bank"] = next_bank
        self.operations.append((user_id, payload, reason))
        return True


@dataclass
class StoreLot:
    lot_id: str
    gun_name: str
    unit_cost: int
    qty_remaining: int


async def _simulate_market_weeks() -> dict[str, object]:
    owner = 101
    buyer_a = 201
    buyer_b = 202

    api = FakeUnbelievaBoat(
        {
            owner: {"cash": 2_000, "bank": 3_000},
            buyer_a: {"cash": 1_000, "bank": 0},
            buyer_b: {"cash": 500, "bank": 0},
        }
    )
    cog = WholesalerCog.__new__(WholesalerCog)
    cog.unbelievaboat = api

    # Simulate attendance/open-shop rewards over time (minted by server economy events).
    minted_total = 0
    for uid in (owner, buyer_a, buyer_b):
        ok = await cog._credit_funds(uid, 250, "Weekly attendance reward")
        assert ok
        minted_total += 250

    lot = StoreLot(lot_id="L-001", gun_name="Nova Revolver", unit_cost=800, qty_remaining=4)

    # Week 1: owner buys 2 units from wholesaler.
    wholesale_total = lot.unit_cost * 2
    deduct_ok, deduct_err = await cog._deduct_funds(owner, wholesale_total, "wh_buy L-001 x2")
    assert deduct_ok, deduct_err

    # Week 2: buyer A purchases one gun from owner.
    sale_total_a = 1_200
    deduct_ok, deduct_err = await cog._deduct_funds(buyer_a, sale_total_a, "sell buyer_a")
    assert deduct_ok, deduct_err
    payout_ok = await cog._credit_funds(owner, sale_total_a, "owner payout buyer_a")
    assert payout_ok
    lot.qty_remaining -= 1

    # Week 3: buyer B attempts a purchase they cannot afford.
    failed_ok, failed_err = await cog._deduct_funds(buyer_b, 2_000, "sell buyer_b_fail")
    assert failed_ok is False
    assert "Insufficient funds" in failed_err

    # Week 4: buyer B makes a valid purchase.
    sale_total_b = 600
    deduct_ok, deduct_err = await cog._deduct_funds(buyer_b, sale_total_b, "sell buyer_b")
    assert deduct_ok, deduct_err
    payout_ok = await cog._credit_funds(owner, sale_total_b, "owner payout buyer_b")
    assert payout_ok
    lot.qty_remaining -= 1

    return {
        "api": api,
        "owner": owner,
        "buyer_a": buyer_a,
        "buyer_b": buyer_b,
        "lot": lot,
        "minted_total": minted_total,
        "wholesale_sink_total": wholesale_total,
    }


def _total_balance(api: FakeUnbelievaBoat, *user_ids: int) -> int:
    return sum(api.balances[uid]["cash"] + api.balances[uid]["bank"] for uid in user_ids)


def test_release_readiness_market_simulation_conserves_money_and_stock():
    result = asyncio.run(_simulate_market_weeks())
    api = result["api"]
    owner = result["owner"]
    buyer_a = result["buyer_a"]
    buyer_b = result["buyer_b"]
    lot = result["lot"]

    # Inventory should never go below zero and should reflect completed sales.
    assert lot.qty_remaining == 2

    # Cross-user transfers should preserve money except for explicitly minted rewards.
    start_total = (2_000 + 3_000) + (1_000 + 0) + (500 + 0)
    end_total = _total_balance(api, owner, buyer_a, buyer_b)
    expected_delta = result["minted_total"] - result["wholesale_sink_total"]
    assert end_total - start_total == expected_delta

    # Ensure UnbelievaBoat interactions were exercised (deduct + credit paths).
    reasons = [reason for _uid, _payload, reason in api.operations]
    assert any("wh_buy" in reason for reason in reasons)
    assert any("owner payout" in reason for reason in reasons)
    assert any("attendance reward" in reason.lower() for reason in reasons)
