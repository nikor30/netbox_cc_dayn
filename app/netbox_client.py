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


def _assignment_entry(data: dict[str, Any]) -> tuple[str | None, str] | None:
    """(role name, contact name) from a contact-assignment record, if usable."""
    contact = data.get("contact")
    contact_name = _record_dict(contact).get("name") if contact is not None else None
    if not contact_name:
        return None
    role = data.get("role")
    role_name = _record_dict(role).get("name") if role is not None else None
    return (str(role_name) if role_name else None, str(contact_name))


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
        self._uplink_ports: dict[int, list[str]] = {}
        self._contacts: dict[int, str | None] = {}
        self._site_vlans: dict[int, list[tuple[int, str]]] = {}
        self._site_contacts: dict[int, list[tuple[str | None, str]]] = {}

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
            self._ingest_interfaces(interfaces, device_ids)

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

    def _ingest_interfaces(
        self, interfaces: list[Any], device_ids: list[int], default_owner: int | None = None
    ) -> None:
        """Fill the connected-device and uplink-port caches from interface records.

        ``default_owner`` attributes interfaces without a device field to the
        single device a per-device query was made for.
        """
        connected: dict[int, list[str]] = {device_id: [] for device_id in device_ids}
        uplinks: dict[int, list[str]] = {device_id: [] for device_id in device_ids}
        for iface in interfaces:
            data = _record_dict(iface)
            owner = data.get("device")
            owner_id = _record_dict(owner).get("id") if owner is not None else None
            if owner_id is None:
                owner_id = default_owner
            if owner_id is None:
                continue
            far_ends = _far_end_names(iface)
            names = connected.setdefault(int(owner_id), [])
            for name in far_ends:
                if name not in names:
                    names.append(name)
            iface_name = data.get("name")
            ports = uplinks.setdefault(int(owner_id), [])
            if far_ends and iface_name and str(iface_name) not in ports:
                ports.append(str(iface_name))
        self._connected.update(connected)
        self._uplink_ports.update(uplinks)

    def _fetch_device_interfaces(self, device_id: int) -> None:
        try:
            interfaces = list(self._api.dcim.interfaces.filter(device_id=device_id, cabled=True))
        except (requests.RequestException, pynetbox.RequestError) as exc:
            raise NetBoxError(f"NetBox interface query failed: {exc}") from exc
        self._ingest_interfaces(interfaces, [device_id], default_owner=device_id)

    def connected_device_names(self, device_id: int) -> list[str]:
        """Far-end device names of all cabled interfaces of a device."""
        if device_id not in self._connected:
            self._fetch_device_interfaces(device_id)
        return self._connected[device_id]

    def uplink_port_names(self, device_id: int) -> list[str]:
        """Local names of the device's cabled (uplink) interfaces."""
        if device_id not in self._uplink_ports:
            self._fetch_device_interfaces(device_id)
        return self._uplink_ports[device_id]

    def prefetch_site_details(self, site_ids: list[int]) -> None:
        """Warm the site VLAN and site contact caches with one query each."""
        if not site_ids:
            return
        try:
            vlans = list(self._api.ipam.vlans.filter(site_id=site_ids))
            grouped: dict[int, list[tuple[int, str]]] = {site_id: [] for site_id in site_ids}
            for vlan in vlans:
                data = _record_dict(vlan)
                site = data.get("site")
                site_id = _record_dict(site).get("id") if site is not None else None
                vid = data.get("vid")
                if site_id is None or vid is None:
                    continue
                grouped.setdefault(int(site_id), []).append(
                    (int(vid), str(data.get("name") or ""))
                )
            for entries in grouped.values():
                entries.sort(key=lambda item: item[0])
            self._site_vlans.update(grouped)

            assignments = list(
                self._api.tenancy.contact_assignments.filter(
                    object_type="dcim.site", object_id=site_ids
                )
            )
            contacts: dict[int, list[tuple[str | None, str]]] = {
                site_id: [] for site_id in site_ids
            }
            for assignment in assignments:
                data = _record_dict(assignment)
                object_id = data.get("object_id")
                entry = _assignment_entry(data)
                if object_id is not None and entry is not None:
                    contacts.setdefault(int(object_id), []).append(entry)
            self._site_contacts.update(contacts)
        except (requests.RequestException, pynetbox.RequestError) as exc:
            logger.warning("netbox site prefetch failed, falling back to per-site: %s", exc)

    def site_vlans(self, site_id: int) -> list[tuple[int, str]]:
        """(vid, name) of every VLAN scoped to a site, sorted by VID."""
        if site_id in self._site_vlans:
            return self._site_vlans[site_id]
        try:
            vlans = list(self._api.ipam.vlans.filter(site_id=site_id))
        except (requests.RequestException, pynetbox.RequestError) as exc:
            raise NetBoxError(f"NetBox VLAN query failed: {exc}") from exc
        entries: list[tuple[int, str]] = []
        for vlan in vlans:
            data = _record_dict(vlan)
            vid = data.get("vid")
            if vid is not None:
                entries.append((int(vid), str(data.get("name") or "")))
        entries.sort(key=lambda item: item[0])
        self._site_vlans[site_id] = entries
        return entries

    def site_contacts(self, site_id: int) -> list[tuple[str | None, str]]:
        """(role name, contact name) of every contact assigned to a site."""
        if site_id in self._site_contacts:
            return self._site_contacts[site_id]
        try:
            assignments = list(
                self._api.tenancy.contact_assignments.filter(
                    object_type="dcim.site", object_id=site_id
                )
            )
        except (requests.RequestException, pynetbox.RequestError) as exc:
            raise NetBoxError(f"NetBox site contact query failed: {exc}") from exc
        entries = [
            entry
            for assignment in assignments
            if (entry := _assignment_entry(_record_dict(assignment))) is not None
        ]
        self._site_contacts[site_id] = entries
        return entries

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

    def status(self) -> dict[str, Any]:
        """NetBox /api/status/ payload; raises NetBoxError when unreachable."""
        try:
            result = self._api.status()
        except Exception as exc:
            raise NetBoxError(f"NetBox is not reachable: {exc}") from exc
        return dict(result) if isinstance(result, dict) else {}

    def ping(self) -> bool:
        """Cheap reachability check for /healthz."""
        try:
            self.status()
        except NetBoxError:
            return False
        return True
