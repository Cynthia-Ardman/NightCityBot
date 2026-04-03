import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from NightCityBot.services.cyberware_shop_data import (
    _normalize_sheet_url,
    _to_int,
    parse_cyberware_sheet,
)


def _run(coro):
    return asyncio.run(coro)


class TestNormalizeSheetUrl:
    def test_empty(self):
        assert _normalize_sheet_url("") == ""
        assert _normalize_sheet_url(None) == ""

    def test_strips_angle_brackets(self):
        url = "<https://docs.google.com/spreadsheets/d/ABC123/edit>"
        result = _normalize_sheet_url(url)
        assert "export" in result
        assert "ABC123" in result

    def test_non_google_url(self):
        url = "https://example.com/sheet.xlsx"
        assert _normalize_sheet_url(url) == url

    def test_already_export(self):
        url = "https://docs.google.com/spreadsheets/d/ABC/export?format=xlsx"
        assert _normalize_sheet_url(url) == url

    def test_edit_url(self):
        url = "https://docs.google.com/spreadsheets/d/ABC123/edit"
        result = _normalize_sheet_url(url)
        assert "export?format=xlsx" in result
        assert "ABC123" in result

    def test_with_gid_query(self):
        url = "https://docs.google.com/spreadsheets/d/ABC123/edit?gid=999"
        result = _normalize_sheet_url(url)
        assert "gid=999" in result

    def test_with_gid_fragment(self):
        url = "https://docs.google.com/spreadsheets/d/ABC123/edit#gid=555"
        result = _normalize_sheet_url(url)
        assert "gid=555" in result

    def test_no_sheet_id_match(self):
        url = "https://docs.google.com/other/page"
        assert _normalize_sheet_url(url) == url

    def test_with_u_prefix(self):
        url = "https://docs.google.com/spreadsheets/u/0/d/XYZ789/edit"
        result = _normalize_sheet_url(url)
        assert "XYZ789" in result
        assert "export" in result


class TestToInt:
    def test_none(self):
        assert _to_int(None) is None

    def test_int(self):
        assert _to_int(42) == 42

    def test_float(self):
        assert _to_int(42.7) == 42

    def test_string_number(self):
        assert _to_int("100") == 100

    def test_string_with_commas(self):
        assert _to_int("1,000") == 1000

    def test_string_with_dollar(self):
        assert _to_int("$500") == 500

    def test_empty_string(self):
        assert _to_int("") is None

    def test_whitespace(self):
        assert _to_int("  ") is None

    def test_non_numeric(self):
        assert _to_int("abc") is None


class TestParseCyberwareSheet:
    def _write_xlsx(self, rows):
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        for row in rows:
            ws.append(row)
        tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
        wb.save(tmp.name)
        wb.close()
        return Path(tmp.name)

    def test_basic_parse(self):
        path = self._write_xlsx([
            ["Item Name", "Price", "CWP", "Description"],
            ["Neural Link", 5000, "3", "Basic neural interface"],
            ["Reflex Booster", 10000, "5", "Speed enhancement"],
        ])
        result = parse_cyberware_sheet(path)
        assert len(result) == 2
        assert result[0]["name"] == "Neural Link"
        assert result[0]["price"] == 5000
        assert result[0]["cwp"] == "3"
        assert result[0]["description"] == "Basic neural interface"

    def test_skips_empty_name(self):
        path = self._write_xlsx([
            ["Name", "Price"],
            ["", 5000],
            [None, 1000],
            ["Valid Item", 2000],
        ])
        result = parse_cyberware_sheet(path)
        assert len(result) == 1
        assert result[0]["name"] == "Valid Item"

    def test_skips_zero_price(self):
        path = self._write_xlsx([
            ["Name", "Pricing"],
            ["Free Item", 0],
            ["Negative", -100],
            ["Paid Item", 500],
        ])
        result = parse_cyberware_sheet(path)
        assert len(result) == 1
        assert result[0]["name"] == "Paid Item"

    def test_empty_sheet(self):
        from openpyxl import Workbook
        wb = Workbook()
        tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
        wb.save(tmp.name)
        wb.close()
        result = parse_cyberware_sheet(Path(tmp.name))
        assert result == []

    def test_alternative_headers(self):
        path = self._write_xlsx([
            ["Cyberware", "Cost", "Cyber Points", "Effect"],
            ["Optics", 3000, "2", "Enhanced vision"],
        ])
        result = parse_cyberware_sheet(path)
        assert len(result) == 1
        assert result[0]["name"] == "Optics"
        assert result[0]["cwp"] == "2"
        assert result[0]["description"] == "Enhanced vision"

    def test_string_price_with_formatting(self):
        path = self._write_xlsx([
            ["Name", "Price"],
            ["Expensive Item", "$15,000"],
        ])
        result = parse_cyberware_sheet(path)
        assert len(result) == 1
        assert result[0]["price"] == 15000

    def test_no_cwp_or_desc_columns(self):
        path = self._write_xlsx([
            ["Name", "Price"],
            ["Simple Item", 1000],
        ])
        result = parse_cyberware_sheet(path)
        assert len(result) == 1
        assert result[0]["cwp"] == ""
        assert result[0]["description"] == ""
