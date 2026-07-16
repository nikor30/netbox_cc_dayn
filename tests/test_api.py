"""Full user journey through the web GUI with mocked NetBox HTTP."""

from pathlib import Path

import pytest
import responses
from fastapi.testclient import TestClient

from app.main import app
from tests.conftest import NETBOX_URL, FakeNetBox, device_payload

FIXTURE = Path(__file__).parent / "fixtures" / "All_templates.csv"
FQDN = "SVEL051CIS.global.web-int.net"


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def upload_id(client: TestClient) -> str:
    response = client.post(
        "/upload",
        files={"file": ("All_templates.csv", FIXTURE.read_bytes(), "text/csv")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return response.headers["location"].rsplit("/", 1)[-1]


def install_netbox(rsps: responses.RequestsMock) -> None:
    FakeNetBox(
        [
            device_payload(
                1,
                FQDN,
                site={"id": 1, "name": "Velizy"},
                location={"id": 2, "name": "Building A / Room 12"},
                rack={"id": 3, "name": "R042"},
                role={"id": 4, "name": "access-switch"},
                asset_tag="WEB-0042",
                tenant={"id": 5, "name": "Webasto IT"},
            )
        ]
    ).install(rsps)
    rsps.add(
        responses.GET,
        f"{NETBOX_URL}/api/dcim/interfaces/",
        json={
            "count": 1,
            "next": None,
            "results": [
                {
                    "id": 10,
                    "url": f"{NETBOX_URL}/api/dcim/interfaces/10/",
                    "connected_endpoints": [
                        {"id": 1, "device": {"id": 9, "name": "CORE1.global.web-int.net"}}
                    ],
                }
            ],
        },
    )
    def contacts_cb(request: object) -> tuple[int, dict[str, str], str]:
        from urllib.parse import parse_qs, urlsplit

        params = parse_qs(urlsplit(request.url).query)  # type: ignore[attr-defined]
        results = []
        if params.get("object_type") == ["dcim.site"]:
            results = [
                {
                    "id": 1,
                    "object_id": 1,
                    "role": {"id": 1, "name": "Local IT"},
                    "contact": {"id": 3, "name": "Ladislav Fekete"},
                }
            ]
        import json

        return (
            200,
            {"Content-Type": "application/json"},
            json.dumps({"count": len(results), "next": None, "results": results}),
        )

    rsps.add_callback(
        responses.GET, f"{NETBOX_URL}/api/tenancy/contact-assignments/", callback=contacts_cb
    )
    rsps.add(
        responses.GET,
        f"{NETBOX_URL}/api/ipam/vlans/",
        json={
            "count": 2,
            "next": None,
            "results": [
                {"id": 1, "vid": 99, "name": "Quarantine", "site": {"id": 1}},
                {"id": 2, "vid": 100, "name": "Medientechnik", "site": {"id": 1}},
            ],
        },
    )


def test_upload_page(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "Upload" in response.text


def test_review_page_lists_devices(client: TestClient, upload_id: str) -> None:
    response = client.get(f"/review/{upload_id}")
    assert response.status_code == 200
    assert FQDN in response.text
    assert "SGIL021CIS.global.web-int.net" in response.text
    assert "critical_vlan_id" in response.text


def test_unknown_session_is_friendly_error(client: TestClient) -> None:
    response = client.get("/review/doesnotexist")
    assert response.status_code == 400
    assert "expired" in response.text


def test_upload_rejects_garbage(client: TestClient) -> None:
    response = client.post("/upload", files={"file": ("x.csv", b'"nope"\n', "text/csv")})
    assert response.status_code == 400
    assert "Could not parse" in response.text


def test_upload_rejects_oversize(client: TestClient) -> None:
    big = b'"x"' * 1_000_000  # ~3 MB
    response = client.post("/upload", files={"file": ("big.csv", big, "text/csv")})
    assert response.status_code == 400
    assert "larger" in response.text


def test_full_journey_fill_manual_export(
    client: TestClient, upload_id: str, rsps: responses.RequestsMock
) -> None:
    install_netbox(rsps)

    # Run NetBox auto-fill: SVEL matches, SGIL does not exist in NetBox.
    response = client.post(f"/review/{upload_id}/fill")
    assert response.status_code == 200
    assert "Velizy" in response.text
    assert "not found" in response.text

    # Manually fill one variable on one block.
    response = client.post(
        f"/review/{upload_id}/value",
        data={"block_index": "1", "variable": "primaryvlan", "value": "42"},
    )
    assert response.status_code == 200

    # Bulk-apply a value to all devices that have patch_field.
    response = client.post(
        f"/review/{upload_id}/value",
        data={
            "block_index": "0",
            "variable": "patch_field",
            "value": "PF-07",
            "apply_all": "true",
        },
    )
    assert response.status_code == 200

    # Export: same block structure, auto-filled + manual values present.
    response = client.get(f"/export/{upload_id}")
    assert response.status_code == 200
    disposition = response.headers["content-disposition"]
    assert "attachment" in disposition
    assert "All_templates_enriched.csv" in disposition

    body = response.content
    assert b'"Velizy"' in body  # NetBox auto-fill
    assert b'"Ladislav Fekete"' in body  # site contact with role "Local IT"
    assert b'"(99,Quarantine);(100,Medientechnik)"' in body  # site VLANs
    assert b'"42"' in body  # manual value
    assert body.count(b'"PF-07"') == 2  # applied to both devices
    # File values are never overwritten (uplink block was fully set in the file).
    assert b'"299","299","299","299"' in body
    # Untouched umlaut values survive byte-exact.
    assert "Gilching Süd".encode() in body
    # Still valid Day-N format: one header per data row.
    assert body.count(b'"Project Name:Template Name"') == 6


def test_fill_survives_netbox_unreachable(
    client: TestClient, upload_id: str, rsps: responses.RequestsMock
) -> None:
    import requests

    rsps.add(
        responses.GET,
        f"{NETBOX_URL}/api/dcim/devices/",
        body=requests.ConnectionError("connection refused"),
    )
    response = client.post(f"/review/{upload_id}/fill")
    assert response.status_code == 200
    assert "NetBox unreachable" in response.text
    # Export still works, with the original content untouched.
    export = client.get(f"/export/{upload_id}")
    assert export.status_code == 200
    assert export.content == FIXTURE.read_bytes()


def test_healthz(client: TestClient, rsps: responses.RequestsMock) -> None:
    rsps.add(responses.GET, f"{NETBOX_URL}/api/status/", json={"netbox-version": "4.1"})
    response = client.get("/healthz")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["netbox"] in ("ok", "unreachable")
