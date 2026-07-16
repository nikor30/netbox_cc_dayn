"""Value resolvers: dotted attribute paths and special NetBox lookups."""

from dataclasses import dataclass, field
from typing import Any

from app.netbox_client import NetBoxClient, NetBoxError

CONNECTED_DEVICE = "connected_device"
PRIMARY_CONTACT = "device.primary_contact"


@dataclass
class Resolved:
    """Outcome of resolving one variable from NetBox."""

    value: str | None = None
    candidates: list[str] = field(default_factory=list)

    @property
    def is_ambiguous(self) -> bool:
        return self.value is None and len(self.candidates) > 1


def _attr(obj: Any, name: str) -> Any:
    """Attribute access that never triggers pynetbox lazy HTTP fetches."""
    data = getattr(obj, "__dict__", None)
    if isinstance(data, dict) and name in data:
        return data[name]
    return None


def resolve_dotted(record: Any, path: str) -> Resolved:
    """Resolve e.g. ``device.site.name`` against a device record.

    The leading ``device.`` segment refers to the record itself. Any ``None``
    along the path resolves to "no value" — never an exception.
    """
    parts = path.split(".")
    if parts and parts[0] == "device":
        parts = parts[1:]
    current: Any = record
    for part in parts:
        if current is None:
            return Resolved()
        parent = current
        current = _attr(parent, part)
        # NetBox 3.6 renamed device_role to role; accept mappings written
        # against either name.
        if current is None and part == "role":
            current = _attr(parent, "device_role")
        elif current is None and part == "device_role":
            current = _attr(parent, "role")
    if current is None or current == "":
        return Resolved()
    return Resolved(value=str(current))


def resolve_connected_device(client: NetBoxClient, device_id: int) -> Resolved:
    """Far-end device of the uplink cabling.

    One cabled far-end device -> that's the uplink switch. Several distinct
    far ends -> ambiguous, the user picks in the GUI. None -> manual.
    """
    try:
        names = client.connected_device_names(device_id)
    except NetBoxError:
        return Resolved()
    if len(names) == 1:
        return Resolved(value=names[0])
    if len(names) > 1:
        return Resolved(candidates=names)
    return Resolved()


def resolve_primary_contact(client: NetBoxClient, record: Any) -> Resolved:
    """Contact assigned to the device; falls back to the tenant name."""
    device_id = _attr(record, "id")
    if device_id is not None:
        try:
            contact = client.primary_contact(int(device_id))
        except NetBoxError:
            contact = None
        if contact:
            return Resolved(value=contact)
    return resolve_dotted(record, "device.tenant.name")


def resolve(source: str, record: Any, client: NetBoxClient) -> Resolved:
    """Dispatch a mapping source string to the right resolver."""
    if source == CONNECTED_DEVICE:
        device_id = _attr(record, "id")
        if device_id is None:
            return Resolved()
        return resolve_connected_device(client, int(device_id))
    if source == PRIMARY_CONTACT:
        return resolve_primary_contact(client, record)
    return resolve_dotted(record, source)
