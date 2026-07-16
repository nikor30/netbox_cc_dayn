"""Resolver tests: dotted paths, connected_device, primary_contact."""

from types import SimpleNamespace

import responses

from app.config import Settings
from app.netbox_client import NetBoxClient
from app.resolvers import (
    Resolved,
    resolve,
    resolve_connected_device,
    resolve_dotted,
    resolve_primary_contact,
    resolve_site_contact,
    resolve_site_vlans,
    resolve_uplink_ports,
)
from tests.conftest import NETBOX_URL


def fake_device(**kwargs: object) -> SimpleNamespace:
    return SimpleNamespace(id=1, name="SVEL051CIS", **kwargs)


def test_dotted_path_resolves() -> None:
    device = fake_device(site=SimpleNamespace(name="Stockdorf"))
    assert resolve_dotted(device, "device.site.name").value == "Stockdorf"


def test_dotted_path_none_mid_path_is_no_value() -> None:
    device = fake_device(site=None)
    resolved = resolve_dotted(device, "device.site.name")
    assert resolved.value is None
    assert not resolved.is_ambiguous


def test_dotted_path_missing_attribute_is_no_value() -> None:
    assert resolve_dotted(fake_device(), "device.rack.name").value is None


def test_dotted_path_empty_string_is_no_value() -> None:
    assert resolve_dotted(fake_device(asset_tag=""), "device.asset_tag").value is None


def test_role_falls_back_to_device_role() -> None:
    device = fake_device(device_role=SimpleNamespace(name="access-switch"))
    assert resolve_dotted(device, "device.role.name").value == "access-switch"


def test_ambiguous_property() -> None:
    assert Resolved(candidates=["a", "b"]).is_ambiguous
    assert not Resolved(value="a", candidates=["a", "b"]).is_ambiguous
    assert not Resolved().is_ambiguous


def _interfaces_payload(endpoints: list[str]) -> dict[str, object]:
    return {
        "count": 1,
        "next": None,
        "results": [
            {
                "id": 10,
                "name": "TenGigE1/1/1",
                "url": f"{NETBOX_URL}/api/dcim/interfaces/10/",
                "connected_endpoints": [
                    {
                        "id": 100 + i,
                        "name": "Te1/0/1",
                        "device": {"id": 50 + i, "name": name},
                    }
                    for i, name in enumerate(endpoints)
                ],
            }
        ],
    }


def test_connected_device_single(settings: Settings, rsps: responses.RequestsMock) -> None:
    rsps.add(
        responses.GET,
        f"{NETBOX_URL}/api/dcim/interfaces/",
        json=_interfaces_payload(["CORE1.global.web-int.net"]),
    )
    resolved = resolve_connected_device(NetBoxClient(settings), 1)
    assert resolved.value == "CORE1.global.web-int.net"


def test_connected_device_multiple_is_ambiguous(
    settings: Settings, rsps: responses.RequestsMock
) -> None:
    rsps.add(
        responses.GET,
        f"{NETBOX_URL}/api/dcim/interfaces/",
        json=_interfaces_payload(["CORE1", "CORE2"]),
    )
    resolved = resolve_connected_device(NetBoxClient(settings), 1)
    assert resolved.value is None
    assert resolved.is_ambiguous
    assert resolved.candidates == ["CORE1", "CORE2"]


def test_connected_device_none(settings: Settings, rsps: responses.RequestsMock) -> None:
    rsps.add(
        responses.GET,
        f"{NETBOX_URL}/api/dcim/interfaces/",
        json={"count": 0, "next": None, "results": []},
    )
    assert resolve_connected_device(NetBoxClient(settings), 1).value is None


def test_connected_device_netbox_error_degrades(settings: Settings) -> None:
    with responses.RequestsMock():
        resolved = resolve_connected_device(NetBoxClient(settings), 1)
    assert resolved.value is None
    assert not resolved.is_ambiguous


def test_primary_contact_from_assignment(
    settings: Settings, rsps: responses.RequestsMock
) -> None:
    rsps.add(
        responses.GET,
        f"{NETBOX_URL}/api/tenancy/contact-assignments/",
        json={
            "count": 1,
            "next": None,
            "results": [{"id": 7, "contact": {"id": 3, "name": "IT Ops Velizy"}}],
        },
    )
    resolved = resolve_primary_contact(NetBoxClient(settings), fake_device())
    assert resolved.value == "IT Ops Velizy"


def test_primary_contact_falls_back_to_tenant(
    settings: Settings, rsps: responses.RequestsMock
) -> None:
    rsps.add(
        responses.GET,
        f"{NETBOX_URL}/api/tenancy/contact-assignments/",
        json={"count": 0, "next": None, "results": []},
    )
    device = fake_device(tenant=SimpleNamespace(name="Webasto IT"))
    assert resolve_primary_contact(NetBoxClient(settings), device).value == "Webasto IT"


def _sited_device() -> SimpleNamespace:
    return fake_device(site=SimpleNamespace(id=5, name="VEL"))


def test_site_vlans_formatted_like_the_file(
    settings: Settings, rsps: responses.RequestsMock
) -> None:
    rsps.add(
        responses.GET,
        f"{NETBOX_URL}/api/ipam/vlans/",
        json={
            "count": 3,
            "next": None,
            "results": [
                {"id": 1, "vid": 100, "name": "Medientechnik", "site": {"id": 5}},
                {"id": 2, "vid": 99, "name": "Quarantine", "site": {"id": 5}},
                {"id": 3, "vid": 102, "name": "GLT", "site": {"id": 5}},
            ],
        },
    )
    resolved = resolve_site_vlans(NetBoxClient(settings), _sited_device())
    assert resolved.value == "(99,Quarantine);(100,Medientechnik);(102,GLT)"


def test_site_vlans_no_site_or_no_vlans(
    settings: Settings, rsps: responses.RequestsMock
) -> None:
    assert resolve_site_vlans(NetBoxClient(settings), fake_device()).value is None
    rsps.add(
        responses.GET,
        f"{NETBOX_URL}/api/ipam/vlans/",
        json={"count": 0, "next": None, "results": []},
    )
    assert resolve_site_vlans(NetBoxClient(settings), _sited_device()).value is None


def test_uplink_ports_from_cabled_interfaces(
    settings: Settings, rsps: responses.RequestsMock
) -> None:
    rsps.add(
        responses.GET,
        f"{NETBOX_URL}/api/dcim/interfaces/",
        json={
            "count": 2,
            "next": None,
            "results": [
                {
                    "id": 10,
                    "name": "Te1/1/3",
                    "device": {"id": 1, "name": "SVEL051CIS"},
                    "connected_endpoints": [{"id": 1, "device": {"id": 9, "name": "SWV_1"}}],
                },
                {
                    "id": 11,
                    "name": "Te1/1/4",
                    "device": {"id": 1, "name": "SVEL051CIS"},
                    "connected_endpoints": [{"id": 2, "device": {"id": 8, "name": "SWV_2"}}],
                },
            ],
        },
    )
    client = NetBoxClient(settings)
    assert resolve_uplink_ports(client, fake_device()).value == "Te1/1/3,Te1/1/4"
    # Same interface query also feeds the connected-device resolver: two
    # distinct far ends -> ambiguous, no extra HTTP call.
    resolved = resolve_connected_device(client, 1)
    assert resolved.candidates == ["SWV_1", "SWV_2"]


def _site_assignments(entries: list[tuple[str | None, str]]) -> dict[str, object]:
    return {
        "count": len(entries),
        "next": None,
        "results": [
            {
                "id": i,
                "object_id": 5,
                "role": {"id": 1, "name": role} if role else None,
                "contact": {"id": 100 + i, "name": name},
            }
            for i, (role, name) in enumerate(entries)
        ],
    }


def test_site_contact_picks_matching_role(
    settings: Settings, rsps: responses.RequestsMock
) -> None:
    rsps.add(
        responses.GET,
        f"{NETBOX_URL}/api/tenancy/contact-assignments/",
        json=_site_assignments(
            [("Site Manager", "Somebody Else"), ("Local IT", "Ladislav Fekete")]
        ),
    )
    resolved = resolve_site_contact(NetBoxClient(settings), _sited_device(), "Local IT")
    assert resolved.value == "Ladislav Fekete"


def test_site_contact_role_case_insensitive(
    settings: Settings, rsps: responses.RequestsMock
) -> None:
    rsps.add(
        responses.GET,
        f"{NETBOX_URL}/api/tenancy/contact-assignments/",
        json=_site_assignments([("local it", "Ladislav Fekete")]),
    )
    resolved = resolve_site_contact(NetBoxClient(settings), _sited_device(), "Local IT")
    assert resolved.value == "Ladislav Fekete"


def test_site_contact_multiple_matches_ambiguous(
    settings: Settings, rsps: responses.RequestsMock
) -> None:
    rsps.add(
        responses.GET,
        f"{NETBOX_URL}/api/tenancy/contact-assignments/",
        json=_site_assignments([("Local IT", "Person A"), ("Local IT", "Person B")]),
    )
    resolved = resolve_site_contact(NetBoxClient(settings), _sited_device(), "Local IT")
    assert resolved.is_ambiguous
    assert resolved.candidates == ["Person A", "Person B"]


def test_site_contact_falls_back_to_tenant(
    settings: Settings, rsps: responses.RequestsMock
) -> None:
    rsps.add(
        responses.GET,
        f"{NETBOX_URL}/api/tenancy/contact-assignments/",
        json={"count": 0, "next": None, "results": []},
    )
    device = _sited_device()
    device.tenant = SimpleNamespace(name="Webasto IT")
    resolved = resolve_site_contact(NetBoxClient(settings), device, "Local IT")
    assert resolved.value == "Webasto IT"


def test_resolve_dispatch(settings: Settings, rsps: responses.RequestsMock) -> None:
    rsps.add(
        responses.GET,
        f"{NETBOX_URL}/api/dcim/interfaces/",
        json=_interfaces_payload(["CORE1"]),
    )
    client = NetBoxClient(settings)
    device = fake_device(site=SimpleNamespace(name="Velizy"))
    assert resolve("device.site.name", device, client).value == "Velizy"
    assert resolve("connected_device", device, client).value == "CORE1"
