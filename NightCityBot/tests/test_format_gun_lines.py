"""Tests for format_gun_lines_grouped helper."""

from NightCityBot.utils.helpers import format_gun_lines_grouped


def _lot(name, weapon_type, restriction="basic", gun_level="M", gun_category="Power", cost=1000, qty=1):
    return {
        "gun_name": name,
        "weapon_type": weapon_type,
        "restriction": restriction,
        "gun_level": gun_level,
        "gun_category": gun_category,
        "unit_cost": cost,
        "qty_available": qty,
    }


class TestFormatGunLinesGrouped:
    def test_groups_by_weapon_type_with_headers(self):
        lots = [
            _lot("Tamayura", "pistol"),
            _lot("DB-2 Testera", "shotgun"),
            _lot("Unity", "pistol"),
        ]
        lines = format_gun_lines_grouped(lots)
        text = "\n".join(lines)
        assert "▬▬ Pistols ▬▬" in text
        assert "▬▬ Shotguns ▬▬" in text
        pistol_idx = text.index("Pistols")
        shotgun_idx = text.index("Shotguns")
        assert pistol_idx < shotgun_idx

    def test_other_bucket_for_missing_weapon_type(self):
        lots = [
            _lot("Mystery Gun", ""),
            _lot("Tamayura", "pistol"),
        ]
        lines = format_gun_lines_grouped(lots)
        text = "\n".join(lines)
        assert "▬▬ Pistols ▬▬" in text
        assert "▬▬ Other ▬▬" in text
        pistol_idx = text.index("Pistols")
        other_idx = text.index("Other")
        assert pistol_idx < other_idx

    def test_other_bucket_for_unknown_weapon_type(self):
        lots = [_lot("Unknown", "plasma_cannon")]
        lines = format_gun_lines_grouped(lots)
        text = "\n".join(lines)
        assert "▬▬ Other ▬▬" in text
        assert "Unknown" in text

    def test_full_power_level_words(self):
        lots = [
            _lot("Gun L", "pistol", gun_level="L"),
            _lot("Gun M", "pistol", gun_level="M"),
            _lot("Gun H", "pistol", gun_level="H"),
        ]
        lines = format_gun_lines_grouped(lots)
        text = "\n".join(lines)
        assert "[Low]" in text
        assert "[Medium]" in text
        assert "[High]" in text
        assert "[L]" not in text
        assert "[M]" not in text
        assert "[H]" not in text

    def test_always_shows_basic_restriction(self):
        lots = [_lot("BasicGun", "pistol", restriction="basic")]
        lines = format_gun_lines_grouped(lots)
        text = "\n".join(lines)
        assert "[Basic]" in text

    def test_all_three_tags_always_present(self):
        lots = [_lot("TestGun", "pistol", restriction="controlled", gun_level="H", gun_category="Smart")]
        lines = format_gun_lines_grouped(lots)
        text = "\n".join(lines)
        assert "[Controlled]" in text
        assert "[High]" in text
        assert "[Smart]" in text
        assert "·" in text

    def test_max_items_truncation(self):
        lots = [_lot(f"Gun{i}", "pistol", qty=1) for i in range(10)]
        lines = format_gun_lines_grouped(lots, max_items=3)
        gun_lines = [l for l in lines if l.startswith("`")]
        assert len(gun_lines) == 3

    def test_numbering_is_sequential_across_groups(self):
        lots = [
            _lot("Pistol1", "pistol"),
            _lot("Shotgun1", "shotgun"),
        ]
        lines = format_gun_lines_grouped(lots)
        assert any("`1.`" in l for l in lines)
        assert any("`2.`" in l for l in lines)

    def test_skips_zero_qty(self):
        lots = [_lot("EmptyGun", "pistol", qty=0)]
        lines = format_gun_lines_grouped(lots)
        assert len(lines) == 0

    def test_qty_key_parameter(self):
        lots = [{"gun_name": "Test", "weapon_type": "pistol", "restriction": "basic",
                 "gun_level": "M", "gun_category": "Power", "unit_cost": 1000, "qty_remaining": 5}]
        lines = format_gun_lines_grouped(lots, qty_key="qty_remaining")
        text = "\n".join(lines)
        assert "× 5" in text

    def test_correct_format_structure(self):
        lots = [_lot("Tamayura", "pistol", restriction="basic", gun_level="M", gun_category="Power", cost=1200, qty=2)]
        lines = format_gun_lines_grouped(lots)
        gun_line = [l for l in lines if "Tamayura" in l][0]
        assert "**Tamayura** — [Basic] · [Medium] · [Power] — $1,200 × 2" in gun_line

    def test_group_ordering_follows_gun_class_order(self):
        lots = [
            _lot("Sniper", "sniper_rifle"),
            _lot("Pistol", "pistol"),
            _lot("SMG", "submachine_gun"),
            _lot("Assault", "assault_rifle"),
        ]
        lines = format_gun_lines_grouped(lots)
        text = "\n".join(lines)
        positions = {
            "pistol": text.index("Pistols"),
            "smg": text.index("Submachine Guns"),
            "ar": text.index("Assault Rifles"),
            "sniper": text.index("Sniper Rifles"),
        }
        assert positions["pistol"] < positions["smg"] < positions["ar"] < positions["sniper"]
