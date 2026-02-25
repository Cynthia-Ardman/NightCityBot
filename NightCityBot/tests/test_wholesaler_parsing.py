from pathlib import Path

from openpyxl import Workbook

from NightCityBot.cogs.wholesaler import WholesalerCog


def test_parse_master_sheet_filters_headers_and_categories(tmp_path: Path):
    wb = Workbook()
    ws = wb.active
    ws.title = "Master Gun List"
    ws.append(["Gun Name", "Type/Armor Effectiveness", "Mag Size", "Price New", "Cyberware Needed"])
    ws.append(["Light Machine Guns", "", "", "", ""])
    ws.append(["Militech Viper", "Power (L)", 30, 1500, "None"])
    ws.append(["Type", "Type", "Mag Size", "Price New", "Cyberware Needed"])
    ws.append(["Arasaka Fang", "Smart (H)", "20", "$4200", 2])
    ws.append(["NoPrice Gun", "Tech (M)", 10, 0, ""])

    xlsx = tmp_path / "guns.xlsx"
    wb.save(xlsx)

    rows = WholesalerCog.parse_master_sheet(xlsx, "Master Gun List")

    assert len(rows) == 2
    assert rows[0]["gun_name"] == "Militech Viper"
    assert rows[0]["gun_level"] == "L"
    assert rows[1]["gun_name"] == "Arasaka Fang"
    assert rows[1]["gun_level"] == "H"
    assert rows[1]["price_new"] == 4200


def test_level_derivation_treats_mh_as_h():
    assert WholesalerCog._derive_level("Tech (M-H)") == "H"
    assert WholesalerCog._derive_level("Tech (M)") == "M"
    assert WholesalerCog._derive_level("Power (L)") == "L"


def test_normalize_shop_name_for_aliases():
    assert WholesalerCog._normalize_shop_name("Shop 1") == "shop-1"
    assert WholesalerCog._normalize_shop_name("SHOP__2") == "shop-2"


def test_restock_settings_allow_zero_for_level_lots():
    cog = WholesalerCog.__new__(WholesalerCog)
    cog.DEFAULT_RESTOCK_SETTINGS = WholesalerCog.DEFAULT_RESTOCK_SETTINGS

    cfg = cog._resolve_restock_settings(
        {
            "settings": {
                "restock": {
                    "total_lots": 5,
                    "lots_L": 5,
                    "lots_M": 0,
                    "lots_H": 0,
                    "qty_min_L": 2,
                    "qty_max_L": 2,
                }
            }
        }
    )

    assert cfg["lots_M"] == 0
    assert cfg["lots_H"] == 0
    assert cfg["total_lots"] == 5


def test_generate_restock_lots_uses_actual_level_on_fallback():
    cog = WholesalerCog.__new__(WholesalerCog)
    guns = [{"gun_name": "Only L", "gun_level": "L", "price_new": 1000}]
    cfg = {
        "total_lots": 1,
        "lots_L": 0,
        "lots_M": 0,
        "lots_H": 1,
        "qty_min_L": 3,
        "qty_max_L": 3,
        "qty_min_M": 1,
        "qty_max_M": 1,
        "qty_min_H": 1,
        "qty_max_H": 1,
    }

    import random

    lots, _totals = cog._generate_restock_lots(guns, cfg, random.Random(1))

    assert len(lots) == 1
    assert lots[0]["gun_level"] == "L"
    assert lots[0]["qty_available"] == 3


def test_generate_restock_lots_recomputes_level_totals_after_cap():
    cog = WholesalerCog.__new__(WholesalerCog)
    guns = [
        {"gun_name": "L1", "gun_level": "L", "price_new": 100},
        {"gun_name": "M1", "gun_level": "M", "price_new": 200},
        {"gun_name": "H1", "gun_level": "H", "price_new": 300},
    ]
    cfg = {
        "total_lots": 1,
        "lots_L": 1,
        "lots_M": 1,
        "lots_H": 1,
        "qty_min_L": 2,
        "qty_max_L": 2,
        "qty_min_M": 3,
        "qty_max_M": 3,
        "qty_min_H": 4,
        "qty_max_H": 4,
    }

    import random

    lots, totals = cog._generate_restock_lots(guns, cfg, random.Random(2))

    assert len(lots) == 1
    level = lots[0]["gun_level"]
    assert totals[level] == lots[0]["qty_available"]
    assert sum(totals.values()) == lots[0]["qty_available"]


def test_normalize_google_sheet_url_to_xlsx_export():
    url = "https://docs.google.com/spreadsheets/d/abc123/edit?usp=sharing"
    out = WholesalerCog._normalize_sheet_source_url(url)
    assert out == "https://docs.google.com/spreadsheets/d/abc123/export?format=xlsx"


def test_normalize_google_sheet_url_preserves_gid_from_fragment():
    url = "https://docs.google.com/spreadsheets/d/abc123/edit#gid=456"
    out = WholesalerCog._normalize_sheet_source_url(url)
    assert out == "https://docs.google.com/spreadsheets/d/abc123/export?format=xlsx&gid=456"


def test_normalize_google_sheet_url_handles_u_path_variant():
    url = "https://docs.google.com/spreadsheets/u/0/d/abc123/edit?usp=sharing"
    out = WholesalerCog._normalize_sheet_source_url(url)
    assert out == "https://docs.google.com/spreadsheets/d/abc123/export?format=xlsx"


def test_normalize_google_sheet_url_strips_discord_angle_brackets():
    url = "<https://docs.google.com/spreadsheets/d/abc123/edit?usp=sharing>"
    out = WholesalerCog._normalize_sheet_source_url(url)
    assert out == "https://docs.google.com/spreadsheets/d/abc123/export?format=xlsx"


def test_normalize_google_sheet_url_parses_gid_with_extra_fragment_args():
    url = "https://docs.google.com/spreadsheets/d/abc123/edit#gid=456&range=A1"
    out = WholesalerCog._normalize_sheet_source_url(url)
    assert out == "https://docs.google.com/spreadsheets/d/abc123/export?format=xlsx&gid=456"


def test_is_admin_allows_discord_administrator_permission():
    cog = WholesalerCog.__new__(WholesalerCog)

    class _Perms:
        administrator = True

    class _Role:
        id = 0

    class _Member:
        guild_permissions = _Perms()
        roles = [_Role()]

    assert cog._is_admin(_Member()) is True


def test_resolve_sheet_path_accepts_url_in_legacy_xlsx_path(monkeypatch):
    import asyncio

    cog = WholesalerCog.__new__(WholesalerCog)
    cog.lock = asyncio.Lock()
    cog.sheet_cache_path = Path('/tmp/sheet_cache.xlsx')

    async def _state():
        return {"settings": {}}

    cog._load_state = _state

    monkeypatch.setattr('config.WHOLESALER_GOOGLE_SHEET_XLSX_URL', '')
    monkeypatch.setattr('config.WHOLESALER_XLSX_PATH', 'https://docs.google.com/spreadsheets/d/abc123/edit?usp=sharing')

    async def _run():
        path = await cog._resolve_sheet_path()
        return path

    class _Resp:
        status = 200

        async def read(self):
            return b'test'

    class _GetCtx:
        async def __aenter__(self):
            return _Resp()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class _Session:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def get(self, url):
            assert url == 'https://docs.google.com/spreadsheets/d/abc123/export?format=xlsx'
            return _GetCtx()

    monkeypatch.setattr('aiohttp.ClientSession', _Session)

    path = asyncio.run(_run())
    assert path == cog.sheet_cache_path
