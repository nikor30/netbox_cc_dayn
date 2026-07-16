"""Matcher tests: exact / short-name / ambiguous / not-found / unreachable."""

import requests
import responses

from app.config import Settings
from app.matcher import match_devices, short_host
from app.netbox_client import NetBoxClient
from tests.conftest import NETBOX_URL, FakeNetBox, device_payload

FQDN = "SVEL051CIS.global.web-int.net"


def make_client(settings: Settings) -> NetBoxClient:
    return NetBoxClient(settings)


def test_short_host() -> None:
    assert short_host(FQDN) == "SVEL051CIS"
    assert short_host("plain") == "plain"


def test_exact_fqdn_match(settings: Settings, rsps: responses.RequestsMock) -> None:
    FakeNetBox([device_payload(1, FQDN)]).install(rsps)
    result = match_devices([FQDN], make_client(settings))[FQDN]
    assert result.status == "matched"
    assert result.record.id == 1
    assert result.to_result().netbox_url == f"{NETBOX_URL}/dcim/devices/1/"


def test_short_name_case_insensitive_match(
    settings: Settings, rsps: responses.RequestsMock
) -> None:
    FakeNetBox([device_payload(2, "svel051cis")]).install(rsps)
    result = match_devices([FQDN], make_client(settings))[FQDN]
    assert result.status == "matched"
    assert result.record.id == 2


def test_wildcard_fallback_single_hit(settings: Settings, rsps: responses.RequestsMock) -> None:
    FakeNetBox([device_payload(3, "SVEL051CIS-stack1")]).install(rsps)
    result = match_devices([FQDN], make_client(settings))[FQDN]
    assert result.status == "matched"
    assert result.record.id == 3


def test_wildcard_multiple_hits_is_ambiguous(
    settings: Settings, rsps: responses.RequestsMock
) -> None:
    FakeNetBox(
        [device_payload(3, "SVEL051CIS-stack1"), device_payload(4, "SVEL051CIS-stack2")]
    ).install(rsps)
    result = match_devices([FQDN], make_client(settings))[FQDN]
    assert result.status == "ambiguous"
    assert {c.id for c in result.candidates} == {3, 4}
    assert len(result.to_result().candidates) == 2


def test_no_hits_is_not_found(settings: Settings, rsps: responses.RequestsMock) -> None:
    FakeNetBox([]).install(rsps)
    result = match_devices([FQDN], make_client(settings))[FQDN]
    assert result.status == "not_found"
    assert result.to_result().device_id is None


def test_netbox_unreachable_flags_all_devices(
    settings: Settings, rsps: responses.RequestsMock
) -> None:
    rsps.add(
        responses.GET,
        f"{NETBOX_URL}/api/dcim/devices/",
        body=requests.ConnectionError("connection refused"),
    )
    results = match_devices([FQDN, "other.example.net"], make_client(settings))
    assert all(r.status == "netbox_unreachable" for r in results.values())


def test_batching_one_query_for_all_exact_names(
    settings: Settings, rsps: responses.RequestsMock
) -> None:
    fake = FakeNetBox([device_payload(1, "a.example.net"), device_payload(2, "b.example.net")])
    fake.install(rsps)
    results = match_devices(["a.example.net", "b.example.net"], make_client(settings))
    assert all(r.status == "matched" for r in results.values())
    assert fake.request_count == 1


def test_lookup_cache_deduplicates_queries(
    settings: Settings, rsps: responses.RequestsMock
) -> None:
    fake = FakeNetBox([device_payload(1, FQDN)])
    fake.install(rsps)
    client = make_client(settings)
    match_devices([FQDN], client)
    match_devices([FQDN], client)
    assert fake.request_count == 1
