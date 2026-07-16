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


class NetBoxClient:
    def __init__(self, settings: Settings) -> None:
        if not settings.netbox_url or not settings.netbox_token:
            raise NetBoxError("NetBox is not configured (NETBOX_URL / NETBOX_TOKEN).")
        self._api = pynetbox.api(settings.netbox_url, token=settings.netbox_token)
        self._api.http_session.verify = settings.netbox_verify_ssl
        self._cache: dict[str, list[Any]] = {}

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

    def connected_device_names(self, device_id: int) -> list[str]:
        """Far-end device names of all cabled interfaces of a device."""
        try:
            interfaces = list(self._api.dcim.interfaces.filter(device_id=device_id, cabled=True))
        except (requests.RequestException, pynetbox.RequestError) as exc:
            raise NetBoxError(f"NetBox interface query failed: {exc}") from exc
        names: list[str] = []
        for iface in interfaces:
            for endpoint in getattr(iface, "connected_endpoints", None) or []:
                device = getattr(endpoint, "device", None)
                name = getattr(device, "name", None) if device is not None else None
                if name and name not in names:
                    names.append(name)
        return names

    def primary_contact(self, device_id: int) -> str | None:
        """First contact assigned to the device, if any."""
        try:
            assignments = list(
                self._api.tenancy.contact_assignments.filter(
                    object_type="dcim.device", object_id=device_id
                )
            )
        except (requests.RequestException, pynetbox.RequestError) as exc:
            raise NetBoxError(f"NetBox contact query failed: {exc}") from exc
        for assignment in assignments:
            contact = getattr(assignment, "contact", None)
            name = getattr(contact, "name", None) if contact is not None else None
            if name:
                return str(name)
        return None

    def ping(self) -> bool:
        """Cheap reachability check for /healthz."""
        try:
            self._api.status()
        except Exception:
            return False
        return True
