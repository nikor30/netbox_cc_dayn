"""Shared test helpers: HTTP-level NetBox mocking with `responses`."""

import json
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
import responses

NETBOX_URL = "https://netbox.test"

# The FastAPI app reads settings at import time; make sure tests always see a
# configured (mock) NetBox regardless of the developer's environment, and keep
# runtime settings out of the working tree.
os.environ["NETBOX_URL"] = NETBOX_URL
os.environ["NETBOX_TOKEN"] = "test-token"
os.environ["RUNTIME_SETTINGS_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="dayn-test-"), "runtime_settings.json"
)

from app.config import Settings, get_settings  # noqa: E402

get_settings.cache_clear()


@pytest.fixture(autouse=True)
def isolated_runtime_settings(tmp_path: Path) -> Iterator[None]:
    """Give every test a fresh, empty runtime settings file."""
    import app.main as main

    main.runtime.set_path(tmp_path / "runtime_settings.json")
    yield
    main.runtime.set_path(tmp_path / "runtime_settings.json")


@pytest.fixture
def settings() -> Settings:
    return Settings(
        netbox_url=NETBOX_URL,
        netbox_token="test-token",
        netbox_verify_ssl=True,
        _env_file=None,  # type: ignore[call-arg]
    )


def device_payload(device_id: int, name: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": device_id,
        "name": name,
        "url": f"{NETBOX_URL}/api/dcim/devices/{device_id}/",
        "display": name,
        **extra,
    }


class FakeNetBox:
    """Registers `responses` callbacks that answer NetBox device queries.

    Filtering semantics implemented: name (exact, multi), name__ie
    (case-insensitive exact, multi), name__isw (case-insensitive starts-with).
    """

    def __init__(self, devices: list[dict[str, Any]]) -> None:
        self.devices = devices
        self.request_count = 0

    def install(self, rsps: responses.RequestsMock, with_details: bool = False) -> None:
        rsps.add_callback(
            responses.GET, f"{NETBOX_URL}/api/dcim/devices/", callback=self._devices_cb
        )
        if with_details:
            rsps.add_callback(
                responses.GET, f"{NETBOX_URL}/api/dcim/interfaces/", callback=self._empty_cb
            )
            rsps.add_callback(
                responses.GET,
                f"{NETBOX_URL}/api/tenancy/contact-assignments/",
                callback=self._empty_cb,
            )
        rsps.add(responses.GET, f"{NETBOX_URL}/api/", json={}, headers={"API-Version": "4.1"})

    def _empty_cb(self, request: Any) -> tuple[int, dict[str, str], str]:
        self.request_count += 1
        body = json.dumps({"count": 0, "next": None, "previous": None, "results": []})
        return 200, {"Content-Type": "application/json"}, body

    def _devices_cb(self, request: Any) -> tuple[int, dict[str, str], str]:
        self.request_count += 1
        params = parse_qs(urlsplit(request.url).query)
        hits = self.devices
        if "name" in params:
            wanted = set(params["name"])
            hits = [d for d in hits if d["name"] in wanted]
        elif "name__ie" in params:
            wanted = {n.lower() for n in params["name__ie"]}
            hits = [d for d in hits if d["name"].lower() in wanted]
        elif "name__isw" in params:
            prefix = params["name__isw"][0].lower()
            hits = [d for d in hits if d["name"].lower().startswith(prefix)]
        body = json.dumps({"count": len(hits), "next": None, "previous": None, "results": hits})
        return 200, {"Content-Type": "application/json"}, body


@pytest.fixture
def rsps() -> Iterator[responses.RequestsMock]:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
        yield mock
