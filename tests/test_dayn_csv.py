"""Parser/exporter tests, including the non-negotiable byte-exact round trip."""

from pathlib import Path

import pytest

from app.dayn_csv import DayNCsvError, export, parse

FIXTURE = Path(__file__).parent / "fixtures" / "All_templates.csv"


@pytest.fixture
def fixture_bytes() -> bytes:
    return FIXTURE.read_bytes()


def test_parse_fixture_structure(fixture_bytes: bytes) -> None:
    doc = parse(fixture_bytes)
    assert len(doc.blocks) == 6
    first = doc.blocks[0]
    assert first.template == "IT-DayN:IT-DayN/IT-DayN:Webasto Login Banner"
    assert first.device_name == "SVEL051CIS.global.web-int.net"
    assert first.columns == [
        "site_full_name",
        "building_room",
        "rack_id",
        "device_role",
        "asset_id",
        "patch_field",
        "support_contact",
    ]
    assert all(v == "" for v in first.variables.values())

    port = doc.blocks[1]
    assert port.variables == {
        "critical_vlan_id": "1010",
        "pvlan": "no",
        "primaryvlan": "",
        "secondaryvlan": "",
    }


def test_parse_preserves_mixed_line_endings(fixture_bytes: bytes) -> None:
    doc = parse(fixture_bytes)
    assert doc.blocks[0].header_ending == "\n"
    assert doc.blocks[0].data_ending == "\r\n"


def test_parse_handles_utf8_umlauts(fixture_bytes: bytes) -> None:
    doc = parse(fixture_bytes)
    gil = [b for b in doc.blocks if b.device_name.startswith("SGIL")][0]
    assert gil.variables["site_full_name"] == "Gilching Süd"
    assert gil.variables["building_room"] == "Gebäude 3 / Raum 12"


def test_device_names_unique_in_order(fixture_bytes: bytes) -> None:
    doc = parse(fixture_bytes)
    assert doc.device_names() == [
        "SVEL051CIS.global.web-int.net",
        "SGIL021CIS.global.web-int.net",
    ]


def test_round_trip_is_byte_identical(fixture_bytes: bytes) -> None:
    doc = parse(fixture_bytes)
    assert export(doc) == fixture_bytes


def test_single_change_only_touches_that_cell(fixture_bytes: bytes) -> None:
    doc = parse(fixture_bytes)
    doc.blocks[1].variables["primaryvlan"] = "42"
    out = export(doc)
    original_lines = fixture_bytes.split(b"\n")
    new_lines = out.split(b"\n")
    assert len(original_lines) == len(new_lines)
    diff = [(a, b) for a, b in zip(original_lines, new_lines, strict=True) if a != b]
    assert len(diff) == 1
    old_line, new_line = diff[0]
    assert b'"1010","no","",""' in old_line
    assert b'"1010","no","42",""' in new_line


def test_changed_row_keeps_quoting_and_ending(fixture_bytes: bytes) -> None:
    doc = parse(fixture_bytes)
    doc.blocks[3].variables["arrVLANs"] = "10,20,30"
    out = export(doc)
    expected = (
        b'"IT-DayN:IT-DayN/IT-DayN:IT_DayN_Vlan",'
        b'"SVEL051CIS.global.web-int.net","10,20,30"\r\n'
    )
    assert expected in out


def test_empty_file_raises() -> None:
    with pytest.raises(DayNCsvError, match="empty"):
        parse(b"")
    with pytest.raises(DayNCsvError, match="empty"):
        parse(b"   \n  \r\n")


def test_wrong_first_cell_raises() -> None:
    data = b'"Nope","Device Name","x"\n"tpl","dev",""\r\n'
    with pytest.raises(DayNCsvError, match="Project Name:Template Name"):
        parse(data)


def test_missing_header_row_raises() -> None:
    data = b'"tpl","dev.example.net","1","2"\r\n'
    with pytest.raises(DayNCsvError, match="header"):
        parse(data)


def test_header_without_data_row_raises() -> None:
    data = b'"Project Name:Template Name","Device Name","var1"\n'
    with pytest.raises(DayNCsvError, match="no data row"):
        parse(data)


def test_two_consecutive_headers_raise() -> None:
    data = (
        b'"Project Name:Template Name","Device Name","var1"\n'
        b'"Project Name:Template Name","Device Name","var2"\n'
        b'"tpl","dev",""\r\n'
    )
    with pytest.raises(DayNCsvError, match="no data row"):
        parse(data)


def test_data_row_with_extra_cells_raises() -> None:
    data = (
        b'"Project Name:Template Name","Device Name","var1"\n'
        b'"tpl","dev","a","b"\r\n'
    )
    with pytest.raises(DayNCsvError, match="more cells"):
        parse(data)


def test_short_data_row_padded_with_empty() -> None:
    data = (
        b'"Project Name:Template Name","Device Name","var1","var2"\n'
        b'"tpl","dev","a"\r\n'
    )
    doc = parse(data)
    assert doc.blocks[0].variables == {"var1": "a", "var2": ""}


def test_non_utf8_raises() -> None:
    with pytest.raises(DayNCsvError, match="UTF-8"):
        parse(b'"Project Name:Template Name"\xff\n')
