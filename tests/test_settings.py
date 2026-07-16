"""Settings GUI: admin auth, NetBox config persistence, connection test."""

import pytest
import requests
import responses
from fastapi.testclient import TestClient

import app.main as main
from app.main import app
from app.runtime_settings import hash_password, verify_password
from tests.conftest import NETBOX_URL

ADMIN = ("admin", "s3cret-pass")


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def set_admin_password(password: str = ADMIN[1]) -> None:
    main.runtime.save(admin_password_hash=hash_password(password))


def test_password_hashing_roundtrip() -> None:
    stored = hash_password("hunter22")
    assert verify_password("hunter22", stored)
    assert not verify_password("wrong", stored)
    assert not verify_password("hunter22", "garbage")


def test_first_run_settings_page_is_open_with_warning(client: TestClient) -> None:
    response = client.get("/settings")
    assert response.status_code == 200
    assert "No admin password is set" in response.text


def test_setting_password_locks_the_page(client: TestClient) -> None:
    response = client.post(
        "/settings",
        data={
            "netbox_url": "",
            "new_password": ADMIN[1],
            "new_password_confirm": ADMIN[1],
        },
    )
    assert response.status_code == 200
    assert "Admin password set" in response.text

    assert client.get("/settings").status_code == 401
    assert client.get("/settings", auth=("admin", "wrong")).status_code == 401
    assert client.get("/settings", auth=ADMIN).status_code == 200


def test_password_mismatch_and_too_short_rejected(client: TestClient) -> None:
    response = client.post(
        "/settings", data={"new_password": "abcdefgh", "new_password_confirm": "different"}
    )
    assert response.status_code == 400
    assert "do not match" in response.text
    response = client.post(
        "/settings", data={"new_password": "short", "new_password_confirm": "short"}
    )
    assert response.status_code == 400
    assert "at least 8" in response.text


def test_save_netbox_settings_and_token_never_echoed(client: TestClient) -> None:
    set_admin_password()
    response = client.post(
        "/settings",
        data={
            "netbox_url": "https://netbox.region.example/",
            "netbox_token": "super-secret-token",
            "netbox_verify_ssl": "true",
        },
        auth=ADMIN,
    )
    assert response.status_code == 200
    assert "Settings saved" in response.text
    assert "https://netbox.region.example" in response.text  # trailing slash stripped
    assert "super-secret-token" not in response.text

    page = client.get("/settings", auth=ADMIN)
    assert "super-secret-token" not in page.text
    assert "a token is stored" in page.text

    assert main.current_settings().netbox_url == "https://netbox.region.example"
    assert main.current_settings().netbox_token == "super-secret-token"


def test_empty_token_field_keeps_stored_token(client: TestClient) -> None:
    set_admin_password()
    client.post(
        "/settings",
        data={"netbox_url": NETBOX_URL, "netbox_token": "keep-me"},
        auth=ADMIN,
    )
    client.post(
        "/settings",
        data={"netbox_url": NETBOX_URL, "netbox_token": ""},
        auth=ADMIN,
    )
    assert main.current_settings().netbox_token == "keep-me"


def test_connection_test_success(client: TestClient, rsps: responses.RequestsMock) -> None:
    rsps.add(
        responses.GET,
        f"{NETBOX_URL}/api/status/",
        json={"netbox-version": "4.1.7"},
    )
    response = client.post(
        "/settings/test",
        data={"netbox_url": NETBOX_URL, "netbox_token": "t", "netbox_verify_ssl": "true"},
    )
    assert response.status_code == 200
    assert "Connected" in response.text
    assert "4.1.7" in response.text


def test_connection_test_failure(client: TestClient, rsps: responses.RequestsMock) -> None:
    rsps.add(
        responses.GET,
        f"{NETBOX_URL}/api/status/",
        body=requests.ConnectionError("connection refused"),
    )
    response = client.post(
        "/settings/test",
        data={"netbox_url": NETBOX_URL, "netbox_token": "t"},
    )
    assert response.status_code == 200
    assert "not reachable" in response.text


def test_connection_test_requires_auth_once_configured(client: TestClient) -> None:
    set_admin_password()
    assert client.post("/settings/test", data={}).status_code == 401


def test_runtime_settings_override_env_for_fill(
    client: TestClient, rsps: responses.RequestsMock
) -> None:
    """After saving a different NetBox URL in the GUI, fill talks to it."""
    other = "https://netbox-other.test"
    set_admin_password()
    client.post(
        "/settings",
        data={"netbox_url": other, "netbox_token": "other-token"},
        auth=ADMIN,
    )
    rsps.add(
        responses.GET,
        f"{other}/api/dcim/devices/",
        json={"count": 0, "next": None, "previous": None, "results": []},
    )
    upload = client.post(
        "/upload",
        files={
            "file": (
                "one.csv",
                b'"Project Name:Template Name","Device Name","rack_id"\n'
                b'"tpl","dev.example.net",""\r\n',
                "text/csv",
            )
        },
        follow_redirects=False,
    )
    upload_id = upload.headers["location"].rsplit("/", 1)[-1]
    response = client.post(f"/review/{upload_id}/fill")
    assert response.status_code == 200
    assert "not found" in response.text  # queried the OTHER NetBox, got zero hits


def test_healthz_uses_runtime_settings(
    client: TestClient, rsps: responses.RequestsMock
) -> None:
    other = "https://netbox-runtime.test"
    main.runtime.save(netbox_url=other, netbox_token="t")
    rsps.add(responses.GET, f"{other}/api/status/", json={"netbox-version": "4.1"})
    assert client.get("/healthz").json()["netbox"] == "ok"
