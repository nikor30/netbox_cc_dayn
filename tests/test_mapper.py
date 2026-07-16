"""Mapper tests: default mappings, precedence, degradation to manual."""

from pathlib import Path
from types import SimpleNamespace

import pytest
import responses

from app.config import Settings
from app.dayn_csv import parse
from app.mapper import DEFAULT_MAPPINGS_PATH, MappingConfigError, load_mappings, map_block
from app.matcher import DeviceMatch
from app.models import TemplateBlock
from app.netbox_client import NetBoxClient
from tests.conftest import NETBOX_URL

FQDN = "SVEL051CIS.global.web-int.net"


def make_block(columns: list[str], values: dict[str, str] | None = None) -> TemplateBlock:
    header = '"Project Name:Template Name","Device Name",' + ",".join(
        f'"{c}"' for c in columns
    )
    cells = ['"tpl"', f'"{FQDN}"'] + [f'"{(values or {}).get(c, "")}"' for c in columns]
    data = ",".join(cells)
    doc = parse((header + "\n" + data + "\r\n").encode())
    return doc.blocks[0]


def full_device() -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        name="SVEL051CIS",
        url=f"{NETBOX_URL}/api/dcim/devices/1/",
        site=SimpleNamespace(id=5, name="Velizy"),
        location=SimpleNamespace(name="Building A / Room 12"),
        rack=SimpleNamespace(name="R042"),
        role=SimpleNamespace(name="access-switch"),
        asset_tag="WEB-0042",
        tenant=SimpleNamespace(name="Webasto IT"),
    )


@pytest.fixture
def client(settings: Settings) -> NetBoxClient:
    return NetBoxClient(settings)


def test_load_default_mappings() -> None:
    mappings = load_mappings()
    assert mappings["site_full_name"] == "device.site.name"
    assert mappings["patch_field"] is None
    assert mappings["uplink_switch"] == "connected_device"
    assert mappings["support_contact"] == "site_contact:Local IT"
    assert mappings["arrVLANs"] == "site_vlans"
    assert mappings["uplink_ports"] == "uplink_ports"


def test_load_mappings_rejects_bad_yaml(tmp_path: Path) -> None:
    bad = tmp_path / "m.yaml"
    bad.write_text("just a string")
    with pytest.raises(MappingConfigError, match="top level"):
        load_mappings(bad)
    bad.write_text("var:\n  nosource: x\n")
    with pytest.raises(MappingConfigError, match="source"):
        load_mappings(bad)
    bad.write_text("var:\n  source: [1]\n")
    with pytest.raises(MappingConfigError, match="string or null"):
        load_mappings(bad)


def test_every_default_mapping_resolves_or_degrades(
    client: NetBoxClient, rsps: responses.RequestsMock
) -> None:
    """Given a complete fake device, no default mapping may raise."""
    rsps.add(
        responses.GET,
        f"{NETBOX_URL}/api/tenancy/contact-assignments/",
        json={"count": 0, "next": None, "results": []},
    )
    rsps.add(
        responses.GET,
        f"{NETBOX_URL}/api/dcim/interfaces/",
        json={"count": 0, "next": None, "results": []},
    )
    rsps.add(
        responses.GET,
        f"{NETBOX_URL}/api/ipam/vlans/",
        json={"count": 0, "next": None, "results": []},
    )
    mappings = load_mappings()
    block = make_block(list(mappings.keys()))
    match = DeviceMatch(name=FQDN, status="matched", record=full_device())
    results = map_block(block, match, mappings, client)

    assert results["site_full_name"].status == "auto"
    assert results["site_full_name"].netbox_value == "Velizy"
    assert results["building_room"].netbox_value == "Building A / Room 12"
    assert results["rack_id"].netbox_value == "R042"
    assert results["device_role"].netbox_value == "access-switch"
    assert results["asset_id"].netbox_value == "WEB-0042"
    assert results["support_contact"].netbox_value == "Webasto IT"  # tenant fallback
    assert results["uplink_switch"].status == "missing"  # no cables -> manual input
    assert results["uplink_ports"].status == "missing"  # no cables -> manual input
    assert results["arrVLANs"].status == "missing"  # site has no VLANs -> manual input
    assert results["patch_field"].status == "manual"  # source: null


def test_none_mid_path_degrades_to_missing(client: NetBoxClient) -> None:
    device = full_device()
    device.rack = None
    match = DeviceMatch(name=FQDN, status="matched", record=device)
    results = map_block(make_block(["rack_id"]), match, {"rack_id": "device.rack.name"}, client)
    assert results["rack_id"].status == "missing"
    assert results["rack_id"].final_value == ""


def test_unknown_variable_is_manual(client: NetBoxClient) -> None:
    match = DeviceMatch(name=FQDN, status="matched", record=full_device())
    results = map_block(make_block(["critical_vlan_id"]), match, load_mappings(), client)
    assert results["critical_vlan_id"].status == "manual"


def test_file_value_is_never_overwritten(client: NetBoxClient) -> None:
    match = DeviceMatch(name=FQDN, status="matched", record=full_device())
    block = make_block(["site_full_name"], {"site_full_name": "Velizy"})
    results = map_block(block, match, load_mappings(), client)
    assert results["site_full_name"].status == "file"
    assert results["site_full_name"].final_value == "Velizy"


def test_conflicting_file_value_flagged_but_kept(client: NetBoxClient) -> None:
    match = DeviceMatch(name=FQDN, status="matched", record=full_device())
    block = make_block(["site_full_name"], {"site_full_name": "Somewhere Else"})
    results = map_block(block, match, load_mappings(), client)
    result = results["site_full_name"]
    assert result.status == "conflict"
    assert result.netbox_value == "Velizy"
    assert result.final_value == "Somewhere Else"


def test_manual_value_wins_over_everything(client: NetBoxClient) -> None:
    match = DeviceMatch(name=FQDN, status="matched", record=full_device())
    block = make_block(["site_full_name"], {"site_full_name": "From File"})
    results = map_block(block, match, load_mappings(), client)
    results["site_full_name"].manual_value = "Chosen By User"
    assert results["site_full_name"].final_value == "Chosen By User"


def test_unmatched_device_everything_manual(client: NetBoxClient) -> None:
    match = DeviceMatch(name=FQDN, status="not_found")
    block = make_block(["site_full_name", "rack_id"], {"site_full_name": "Velizy"})
    results = map_block(block, match, load_mappings(), client)
    assert results["site_full_name"].status == "file"
    assert results["rack_id"].status == "manual"


def test_ambiguous_uplink(client: NetBoxClient, rsps: responses.RequestsMock) -> None:
    rsps.add(
        responses.GET,
        f"{NETBOX_URL}/api/dcim/interfaces/",
        json={
            "count": 2,
            "next": None,
            "results": [
                {
                    "id": 10,
                    "url": f"{NETBOX_URL}/api/dcim/interfaces/10/",
                    "connected_endpoints": [{"id": 1, "device": {"id": 5, "name": "CORE1"}}],
                },
                {
                    "id": 11,
                    "url": f"{NETBOX_URL}/api/dcim/interfaces/11/",
                    "connected_endpoints": [{"id": 2, "device": {"id": 6, "name": "CORE2"}}],
                },
            ],
        },
    )
    match = DeviceMatch(name=FQDN, status="matched", record=full_device())
    results = map_block(make_block(["uplink_switch"]), match, load_mappings(), client)
    assert results["uplink_switch"].status == "ambiguous"
    assert results["uplink_switch"].candidates == ["CORE1", "CORE2"]


def test_default_mappings_path_exists() -> None:
    assert DEFAULT_MAPPINGS_PATH.name == "mappings.yaml"
    assert DEFAULT_MAPPINGS_PATH.exists()
