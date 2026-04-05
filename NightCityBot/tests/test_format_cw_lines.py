"""Tests for format_cw_lines_grouped helper."""

from NightCityBot.utils.helpers import format_cw_lines_grouped


def _lot(name, slot="neural", cwp="10", cost=5000, qty=3):
    return {
        "item_name": name,
        "slot": slot,
        "cwp": cwp,
        "unit_cost": cost,
        "qty_available": qty,
    }


class TestFormatCwLinesGrouped:
    def test_groups_by_slot_with_headers(self):
        lots = [
            _lot("Neural Link", slot="neural"),
            _lot("Kiroshi Optics", slot="ocular system"),
            _lot("Sandevistan", slot="neural"),
        ]
        lines = format_cw_lines_grouped(lots)
        text = "\n".join(lines)
        assert "▬▬ Neural ▬▬" in text
        assert "▬▬ Ocular System ▬▬" in text
        neural_idx = text.index("Neural ▬▬")
        ocular_idx = text.index("Ocular System ▬▬")
        assert neural_idx < ocular_idx

    def test_slot_ordering_follows_cw_slot_order(self):
        lots = [
            _lot("Leg Boost", slot="legs & mobility"),
            _lot("Neural Link", slot="neural"),
            _lot("Kiroshi Optics", slot="ocular system"),
            _lot("Skin Weave", slot="integumentary system"),
        ]
        lines = format_cw_lines_grouped(lots)
        text = "\n".join(lines)
        positions = {
            "neural": text.index("Neural ▬▬"),
            "ocular": text.index("Ocular System ▬▬"),
            "integ": text.index("Integumentary System ▬▬"),
            "legs": text.index("Legs & Mobility ▬▬"),
        }
        assert positions["neural"] < positions["ocular"] < positions["integ"] < positions["legs"]

    def test_other_bucket_for_unknown_slot(self):
        lots = [
            _lot("Mystery CW", slot="unknown_slot"),
            _lot("Neural Link", slot="neural"),
        ]
        lines = format_cw_lines_grouped(lots)
        text = "\n".join(lines)
        assert "▬▬ Neural ▬▬" in text
        assert "▬▬ Other ▬▬" in text
        neural_idx = text.index("Neural ▬▬")
        other_idx = text.index("Other ▬▬")
        assert neural_idx < other_idx

    def test_other_bucket_for_empty_slot(self):
        lots = [_lot("No Slot CW", slot="")]
        lines = format_cw_lines_grouped(lots)
        text = "\n".join(lines)
        assert "▬▬ Other ▬▬" in text

    def test_cwp_tag_format(self):
        lots = [_lot("Neural Link", cwp="14")]
        lines = format_cw_lines_grouped(lots)
        text = "\n".join(lines)
        assert "[CWP: 14]" in text

    def test_no_cwp_omits_tag(self):
        lots = [_lot("Plain Item", cwp="")]
        lines = format_cw_lines_grouped(lots)
        text = "\n".join(lines)
        assert "[CWP:" not in text

    def test_correct_format_structure(self):
        lots = [_lot("Neural Link", slot="neural", cwp="14", cost=5000, qty=10)]
        lines = format_cw_lines_grouped(lots)
        item_line = [l for l in lines if "Neural Link" in l][0]
        assert "**Neural Link** — [CWP: 14] — $5,000 × 10" in item_line

    def test_max_items_truncation(self):
        lots = [_lot(f"CW{i}", qty=1) for i in range(10)]
        lines = format_cw_lines_grouped(lots, max_items=3)
        item_lines = [l for l in lines if l.startswith("`")]
        assert len(item_lines) == 3

    def test_numbering_is_sequential_across_groups(self):
        lots = [
            _lot("Neural Link", slot="neural"),
            _lot("Kiroshi Optics", slot="ocular system"),
        ]
        lines = format_cw_lines_grouped(lots)
        assert any("`1.`" in l for l in lines)
        assert any("`2.`" in l for l in lines)

    def test_skips_zero_qty(self):
        lots = [_lot("Empty CW", qty=0)]
        lines = format_cw_lines_grouped(lots)
        assert len(lines) == 0

    def test_sold_out_items_shown_when_enabled(self):
        lots = [
            _lot("Available CW", qty=2),
            _lot("Sold Out CW", qty=0),
        ]
        lines = format_cw_lines_grouped(lots, show_sold_out=True)
        text = "\n".join(lines)
        assert "Sold out" in text
        assert "~~" in text

    def test_sold_out_items_hidden_by_default(self):
        lots = [
            _lot("Available CW", qty=2),
            _lot("Sold Out CW", qty=0),
        ]
        lines = format_cw_lines_grouped(lots)
        text = "\n".join(lines)
        assert "Sold out" not in text

    def test_qty_key_parameter(self):
        lots = [{"item_name": "Test", "slot": "neural", "cwp": "5",
                 "unit_cost": 1000, "count": 7}]
        lines = format_cw_lines_grouped(lots, qty_key="count")
        text = "\n".join(lines)
        assert "× 7" in text

    def test_name_key_parameter(self):
        lots = [{"name": "CustomName", "slot": "neural", "cwp": "5",
                 "unit_cost": 1000, "qty_available": 1}]
        lines = format_cw_lines_grouped(lots, name_key="name")
        text = "\n".join(lines)
        assert "CustomName" in text

    def test_no_restriction_tag(self):
        lots = [_lot("Neural Link", cwp="14")]
        lines = format_cw_lines_grouped(lots)
        text = "\n".join(lines)
        assert "Basic" not in text
        assert "Controlled" not in text
        assert "Restricted" not in text

    def test_all_eleven_slots_display_correctly(self):
        from NightCityBot.utils.constants import CW_SLOT_ORDER, CW_SLOT_DISPLAY_NAMES
        lots = [_lot(f"CW_{slot}", slot=slot) for slot in CW_SLOT_ORDER]
        lines = format_cw_lines_grouped(lots)
        text = "\n".join(lines)
        for slot in CW_SLOT_ORDER:
            display = CW_SLOT_DISPLAY_NAMES[slot]
            assert f"▬▬ {display} ▬▬" in text
