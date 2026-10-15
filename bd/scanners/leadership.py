from __future__ import annotations

from bd.scanners.base import Scanner


class LeadershipScanner(Scanner):
    name = "leadership"
    allowed_types = frozenset({"leadership_change", "leadership_transformation_ai_data", "open_exec_search"})
