"""Applies mappings.yaml to parsed CSV blocks, producing MappingResults."""

import logging
from pathlib import Path

import yaml

from app.matcher import DeviceMatch
from app.models import MappingResult, TemplateBlock
from app.netbox_client import NetBoxClient, web_url
from app.resolvers import resolve

logger = logging.getLogger(__name__)

DEFAULT_MAPPINGS_PATH = Path(__file__).resolve().parent.parent / "mappings.yaml"


class MappingConfigError(Exception):
    """mappings.yaml is malformed."""


def load_mappings(path: Path = DEFAULT_MAPPINGS_PATH) -> dict[str, str | None]:
    """Load and validate the variable -> NetBox source mapping file."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise MappingConfigError(f"Cannot read mapping file {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise MappingConfigError(f"{path}: top level must be a mapping of variable names.")
    mappings: dict[str, str | None] = {}
    for variable, spec in raw.items():
        if not isinstance(spec, dict) or "source" not in spec:
            raise MappingConfigError(f"{path}: entry '{variable}' must have a 'source' key.")
        source = spec["source"]
        if source is not None and not isinstance(source, str):
            raise MappingConfigError(f"{path}: '{variable}.source' must be a string or null.")
        mappings[str(variable)] = source
    return mappings


def map_block(
    block: TemplateBlock,
    match: DeviceMatch | None,
    mappings: dict[str, str | None],
    client: NetBoxClient | None,
) -> dict[str, MappingResult]:
    """Resolve every variable of one template block.

    Statuses: ``auto`` (NetBox filled it), ``file`` (non-empty in the upload,
    NetBox agrees or has nothing), ``conflict`` (file and NetBox disagree),
    ``ambiguous`` (several NetBox candidates), ``missing`` (mapped but NetBox
    has no value), ``manual`` (no NetBox source).
    """
    results: dict[str, MappingResult] = {}
    matched = match is not None and match.status == "matched" and client is not None
    device_url = web_url(match.record) if matched and match is not None else ""

    for variable in block.columns:
        file_value = block.original_variables.get(variable, "")
        result = MappingResult(variable=variable, file_value=file_value)

        source = mappings.get(variable)
        if source is None or not matched:
            result.status = "file" if file_value else "manual"
            results[variable] = result
            continue

        assert match is not None and client is not None
        resolved = resolve(source, match.record, client)
        result.netbox_object_url = device_url

        if resolved.is_ambiguous:
            result.candidates = resolved.candidates
            result.status = "file" if file_value else "ambiguous"
        elif resolved.value is None:
            result.status = "file" if file_value else "missing"
        else:
            result.netbox_value = resolved.value
            if file_value and file_value != resolved.value:
                result.status = "conflict"
            elif file_value:
                result.status = "file"
            else:
                result.status = "auto"
        results[variable] = result

    return results


def map_document_block_results(
    blocks: list[TemplateBlock],
    matches: dict[str, DeviceMatch],
    mappings: dict[str, str | None],
    client: NetBoxClient | None,
) -> list[dict[str, MappingResult]]:
    """Mapping results for every block, in document order."""
    return [
        map_block(block, matches.get(block.device_name), mappings, client) for block in blocks
    ]
