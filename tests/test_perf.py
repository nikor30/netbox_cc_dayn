"""Performance guardrails: batched NetBox queries, fast review rendering."""

import time

import pytest
import responses
from fastapi.testclient import TestClient

from app.main import app
from tests.conftest import FakeNetBox, device_payload

DEVICE_COUNT = 200
HEADER = (
    '"Project Name:Template Name","Device Name","site_full_name","building_room",'
    '"rack_id","device_role","asset_id","patch_field","support_contact"'
)
UPLINK_HEADER = (
    '"Project Name:Template Name","Device Name","uplink_ports","po_id",'
    '"uplink_switch","native_vlan_id"'
)


def synthetic_csv() -> bytes:
    lines: list[str] = []
    for i in range(DEVICE_COUNT):
        fqdn = f"SW{i:04d}.global.web-int.net"
        lines.append(HEADER + "\n")
        lines.append(f'"IT-DayN:IT-DayN/IT-DayN:Banner","{fqdn}","","","","","","",""\r\n')
        lines.append(UPLINK_HEADER + "\n")
        lines.append(f'"IT-DayN:IT-DayN/IT-DayN:Uplink","{fqdn}","","","",""\r\n')
    return "".join(lines).encode()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_200_devices_batched_and_fast(
    client: TestClient, rsps: responses.RequestsMock
) -> None:
    fake = FakeNetBox(
        [
            device_payload(
                i + 1,
                f"SW{i:04d}.global.web-int.net",
                site={"id": 1, "name": "Süddeutschland Hauptstandort"},
                location={"id": 2, "name": "Gebäude 3 / Raum 12"},
                rack={"id": 3, "name": f"R{i:03d}"},
                role={"id": 4, "name": "access-switch"},
                asset_tag=f"WEB-{i:04d}",
            )
            for i in range(DEVICE_COUNT)
        ]
    )
    fake.install(rsps, with_details=True)

    response = client.post(
        "/upload",
        files={"file": ("big.csv", synthetic_csv(), "text/csv")},
        follow_redirects=False,
    )
    upload_id = response.headers["location"].rsplit("/", 1)[-1]

    start = time.perf_counter()
    fill = client.post(f"/review/{upload_id}/fill")
    fill_seconds = time.perf_counter() - start
    assert fill.status_code == 200

    # One devices query + one interfaces prefetch + one contacts prefetch.
    assert fake.request_count <= 3, f"expected batched queries, saw {fake.request_count}"

    start = time.perf_counter()
    review = client.get(f"/review/{upload_id}")
    render_seconds = time.perf_counter() - start
    assert review.status_code == 200
    assert render_seconds < 2.0, f"review render took {render_seconds:.2f}s"
    assert fill_seconds < 5.0, f"fill took {fill_seconds:.2f}s"

    # Umlauts must render as-is (UTF-8 everywhere).
    assert "Süddeutschland Hauptstandort" in review.text

    export = client.get(f"/export/{upload_id}")
    assert export.status_code == 200
    assert export.content.count("Süddeutschland Hauptstandort".encode()) == DEVICE_COUNT
