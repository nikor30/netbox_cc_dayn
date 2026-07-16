"""Pydantic models for the parsed Day-N CSV document and mapping results."""

from typing import Literal

from pydantic import BaseModel, Field

MappingStatus = Literal["auto", "manual", "conflict", "ambiguous", "missing", "file"]
MatchStatus = Literal["matched", "ambiguous", "not_found", "netbox_unreachable"]


class TemplateBlock(BaseModel):
    """One header/data row pair from the Catalyst Center Day-N CSV."""

    template: str
    device_name: str
    columns: list[str]
    variables: dict[str, str]
    # Raw lines (without line endings) and the original endings, kept for
    # byte-exact export of unchanged rows.
    raw_header: str
    raw_data: str
    header_ending: str
    data_ending: str
    original_variables: dict[str, str]


class CsvDocument(BaseModel):
    """The whole uploaded file as an ordered list of template blocks."""

    blocks: list[TemplateBlock] = Field(default_factory=list)

    def device_names(self) -> list[str]:
        """Unique device names in file order."""
        seen: dict[str, None] = {}
        for block in self.blocks:
            seen.setdefault(block.device_name)
        return list(seen)


class MatchCandidate(BaseModel):
    id: int
    name: str
    url: str = ""


class MatchResult(BaseModel):
    device_name: str
    status: MatchStatus
    device_id: int | None = None
    netbox_url: str = ""
    candidates: list[MatchCandidate] = Field(default_factory=list)


class MappingResult(BaseModel):
    """Outcome of resolving one CSV variable for one block."""

    variable: str
    file_value: str = ""
    netbox_value: str | None = None
    manual_value: str | None = None
    status: MappingStatus = "manual"
    netbox_object_url: str = ""
    candidates: list[str] = Field(default_factory=list)

    @property
    def final_value(self) -> str:
        """Precedence: manual > NetBox auto-fill > file value."""
        if self.manual_value is not None:
            return self.manual_value
        if self.file_value:
            return self.file_value
        if self.netbox_value:
            return self.netbox_value
        return ""
