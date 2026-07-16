"""Thin pynetbox wrapper: batch lookups, per-instance caching, typed errors.

One client instance is created per upload session, so its caches naturally
live exactly as long as the upload they belong to.
"""

import logging
from typing import Any

import pynetbox
import requests

from app.config import Settings

logger = logging.getLogger(__name__)


class NetBoxError(Exception):
    """NetBox is unreachable or returned an unusable response."""


def web_url(record: Any) -> str:
    """Best-effort GUI URL for a pynetbox record (API URL without /api).

    Reads the record's already-parsed attributes only — plain ``getattr`` on a
    pynetbox record lazy-fetches missing fields over HTTP.
    """
    data = getattr(record, "__dict__", {})
    display_url = data.get("display_url")
    if display_url:
        return str(display_url)
    api_url = str(data.get("url") or "")
    return api_url.replace("/api/", "/", 1)


def _record_dict(record: Any) -> dict[str, Any]:
    """A record's already-parsed attributes, without lazy HTTP fetches."""
    data = getattr(record, "__dict__", None)
    return data if isinstance(data, dict) else {}


def _far_end_names(iface: Any) -> list[str]:
    names: list[str] = []
    for endpoint in _record_dict(iface).get("connected_endpoints") or []:
        device = _record_dict(endpoint).get("device")
        name = _record_dict(device).get("name") if device is not None else None
        if name:
            names.append(str(name))
    return names


class NetBoxClient:
    def __init__(self, settings: Settings) -> None:
        if not settings.netbox_url or not settings.netbox_token:
            raise NetBoxError("NetBox is not configured (NETBOX_URL / NETBOX_TOKEN).")
        self._api = pynetbox.api(settings.netbox_url, token=settings.netbox_token)
        self._api.http_session.verify = settings.netbox_verify_ssl
        self._cache: dict[str, list[Any]] = {}
        self._connected: dict[int, list[str]] = {}
        self._contacts: dict[int, str | None] = {}

    def _filter_devices(self, cache_key: str, **params: Any) -> list[Any]:
        if cache_key in self._cache:
            return self._cache[cache_key]
        try:
            records = list(self._api.dcim.devices.filter(**params))
        except (requests.RequestException, pynetbox.RequestError) as exc:
            logger.warning("netbox device query failed: %s", exc)
            raise NetBoxError(f"NetBox query failed: {exc}") from exc
        self._cache[cache_key] = records
        return records

    def devices_by_names(self, names: list[str]) -> list[Any]:
        """Batch exact-name lookup (one query for all names)."""
        if not names:
            return []
        return self._filter_devices("name=" + "|".join(sorted(names)), name=names)

    def devices_by_short_names_ci(self, short_names: list[str]) -> list[Any]:
        """Batch case-insensitive exact lookup on short hostnames."""
        if not short_names:
            return []
        return self._filter_devices(
            "name__ie=" + "|".join(sorted(short_names)), name__ie=short_names
        )

    def devices_startswith(self, prefix: str) -> list[Any]:
        """Wildcard fallback: case-insensitive starts-with."""
        return self._filter_devices(f"name__isw={prefix}", name__isw=prefix)

    def prefetch_device_details(self, device_ids: list[int]) -> None:
        """Warm the connected-device and contact caches with one query each.

        Called once per upload after matching so per-variable resolution never
        needs another HTTP round trip. Failures are swallowed — the per-device
        fallback paths handle (or degrade on) them individually.
        """
        if not device_ids:
            return
        try:
            interfaces = list(self._api.dcim.interfaces.filter(device_id=device_ids, cabled=True))
            connected: dict[int, list[str]] = {device_id: [] for device_id in device_ids}
            for iface in interfaces:
                owner = _record_dict(iface).get("device")
                owner_id = _record_dict(owner).get("id") if owner is not None else None
                if owner_id is None:
                    continue
                names = connected.setdefault(int(owner_id), [])
                for name in _far_end_names(iface):
                    if name not in names:
                        names.append(name)
            self._connected.update(connected)

            assignments = list(
                self._api.tenancy.contact_assignments.filter(
                    object_type="dcim.device", object_id=device_ids
                )
            )
            contacts: dict[int, str | None] = {device_id: None for device_id in device_ids}
            for assignment in assignments:
                data = _record_dict(assignment)
                object_id = data.get("object_id")
                contact = data.get("contact")
                contact_name = _record_dict(contact).get("name") if contact is not None else None
                if (
                    object_id is not None
                    and contact_name
                    and contacts.get(int(object_id)) is None
                ):
                    contacts[int(object_id)] = str(contact_name)
            self._contacts.update(contacts)
        except (requests.RequestException, pynetbox.RequestError) as exc:
            logger.warning("netbox prefetch failed, falling back to per-device: %s", exc)

    def connected_device_names(self, device_id: int) -> list[str]:
        """Far-end device names of all cabled interfaces of a device."""
        if device_id in self._connected:
            return self._connected[device_id]
        try:
            interfaces = list(self._api.dcim.interfaces.filter(device_id=device_id, cabled=True))
        except (requests.RequestException, pynetbox.RequestError) as exc:
            raise NetBoxError(f"NetBox interface query failed: {exc}") from exc
        names: list[str] = []
        for iface in interfaces:
            for name in _far_end_names(iface):
                if name not in names:
                    names.append(name)
        self._connected[device_id] = names
        return names

    def primary_contact(self, device_id: int) -> str | None:
        """First contact assigned to the device, if any."""
        if device_id in self._contacts:
            return self._contacts[device_id]
        try:
            assignments = list(
                self._api.tenancy.contact_assignments.filter(
                    object_type="dcim.device", object_id=device_id
                )
            )
        except (requests.RequestException, pynetbox.RequestError) as exc:
            raise NetBoxError(f"NetBox contact query failed: {exc}") from exc
        result: str | None = None
        for assignment in assignments:
            contact = _record_dict(assignment).get("contact")
            name = _record_dict(contact).get("name") if contact is not None else None
            if name:
                result = str(name)
                break
        self._contacts[device_id] = result
        return result

    def ping(self) -> bool:
        """Cheap reachability check for /healthz."""
        try:
            self._api.status()
        except Exception:
            return False
        return True
