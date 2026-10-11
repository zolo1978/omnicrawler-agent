"""Shared reader-report design tokens, distilled from permissive skills.

See docs/report-design.md and docs/references/document-design/manifest.json.
Original implementation; adapted for offline Chinese A4 evidence reports.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ReportTheme:
    body_pt: int = 12
    title_pt: int = 30
    section_pt: int = 20
    subheading_pt: int = 15
    emphasis_pt: int = 14
    evidence_pt: float = 10.5
    ink: str = "#102C46"
    text: str = "#243447"
    accent: str = "#087F8C"
    muted: str = "#5B6F82"
    tint: str = "#EDF5F6"
    rule: str = "#D9E3EB"
    margin_mm: int = 20
    line_height: float = 1.65


THEME = ReportTheme()
