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
