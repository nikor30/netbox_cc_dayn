"""Device name matching: CSV device names (FQDNs) -> NetBox device records.

Strategy per unique name, batched where possible:

1. exact name match on the full FQDN,
2. case-insensitive exact match on the short hostname (first label),
3. case-insensitive starts-with fallback, accepted only if unambiguous.
"""

from dataclasses import dataclass, field
from typing import Any

from app.models import MatchCandidate, MatchResult, MatchStatus
from app.netbox_client import NetBoxClient, NetBoxError, web_url


@dataclass
class DeviceMatch:
    name: str
    status: MatchStatus
    record: Any = None
    candidates: list[Any] = field(default_factory=list)

    def to_result(self) -> MatchResult:
        return MatchResult(
            device_name=self.name,
            status=self.status,
            device_id=int(self.record.id) if self.record is not None else None,
            netbox_url=web_url(self.record) if self.record is not None else "",
            candidates=[
                MatchCandidate(id=int(c.id), name=str(c.name), url=web_url(c))
                for c in self.candidates
            ],
        )


def short_host(fqdn: str) -> str:
    return fqdn.split(".")[0]


def match_devices(names: list[str], client: NetBoxClient) -> dict[str, DeviceMatch]:
    """Resolve every unique CSV device name against NetBox.

    On any NetBox failure all devices are flagged ``netbox_unreachable`` so the
    app keeps working with manual fill only.
    """
    unique = list(dict.fromkeys(names))
    try:
        return _match(unique, client)
    except NetBoxError:
        return {name: DeviceMatch(name=name, status="netbox_unreachable") for name in unique}


def _match(names: list[str], client: NetBoxClient) -> dict[str, DeviceMatch]:
    results: dict[str, DeviceMatch] = {}

    # Stage 1: one batched exact-name query for all FQDNs.
    exact = {str(r.name): r for r in client.devices_by_names(names)}
    unresolved: list[str] = []
    for name in names:
        if name in exact:
            results[name] = DeviceMatch(name=name, status="matched", record=exact[name])
        else:
            unresolved.append(name)

    # Stage 2: one batched case-insensitive query for all short hostnames.
    if unresolved:
        shorts = [short_host(n) for n in unresolved]
        short_records = client.devices_by_short_names_ci(shorts)
        for name in list(unresolved):
            wanted = short_host(name).lower()
            hits = [r for r in short_records if str(r.name).lower() == wanted]
            if len(hits) == 1:
                results[name] = DeviceMatch(name=name, status="matched", record=hits[0])
                unresolved.remove(name)
            elif len(hits) > 1:
                results[name] = DeviceMatch(name=name, status="ambiguous", candidates=hits)
                unresolved.remove(name)

    # Stage 3: per-name wildcard fallback, only accepted when unambiguous.
    for name in unresolved:
        hits = client.devices_startswith(short_host(name))
        if len(hits) == 1:
            results[name] = DeviceMatch(name=name, status="matched", record=hits[0])
        elif len(hits) > 1:
            results[name] = DeviceMatch(name=name, status="ambiguous", candidates=hits)
        else:
            results[name] = DeviceMatch(name=name, status="not_found")

    return results
