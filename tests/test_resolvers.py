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
