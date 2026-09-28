"""Wage-type name -> wage-code mapping (fallback).

The current template carries each line's code in Pay Line Items `wage_code`, which always wins.
This mapping (data/wage_codes.csv: molga,wage_type,wage_code — molga '*' applies to every country
grouping, a molga-specific row overrides it) is only consulted for files in the original template
(no wage_code column) and for blank wage_code cells. Anything still unresolved gets a provisional code
in its sub-category's number range — never one in use in the file or reserved here — and is reported.
"""

import csv
import logging
import threading
from pathlib import Path

log = logging.getLogger(__name__)

WILDCARD = "*"
REQUIRED_COLUMNS = {"molga", "wage_type", "wage_code"}

# Number ranges per line_category for provisional codes: [start, end).
CODE_RANGES: dict[str, tuple[int, int]] = {
    "EARNING": (1000, 1500),
    "IMPUTED_INCOME": (1500, 2000),
    "US_TAX": (4000, 5000),
    "PRE_TAX_DEDUCTION": (5000, 6000),
    "AFTER_TAX_DEDUCTION": (6000, 7000),
    "EMPLOYER_PAID_BENEFIT": (7000, 8000),
}
FALLBACK_RANGE = (9000, 10000)
STEP = 10


class MappingError(RuntimeError):
    """The mapping file itself is broken — a configuration problem, not a user-input problem."""


def normalise_name(name: str) -> str:
    return " ".join(name.split()).casefold()


class WageCodeMap:
    def __init__(self, entries: dict[tuple[str, str], str], issues: list[str] | None = None):
        self._entries = entries
        self.issues = issues or []

    @classmethod
    def load(cls, path: Path) -> "WageCodeMap":
        if not path.is_file():
            raise MappingError(f"Wage-code mapping file not found: {path}")
        entries: dict[tuple[str, str], str] = {}
        names_by_code: dict[tuple[str, str], str] = {}
        issues: list[str] = []
        with path.open(newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            header = {(h or "").strip().lower() for h in reader.fieldnames or []}
            if not header >= REQUIRED_COLUMNS:
                raise MappingError(f"{path.name} must have columns {', '.join(sorted(REQUIRED_COLUMNS))}")
            for line, raw in enumerate(reader, start=2):
                row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
                molga, name, code = row["molga"] or WILDCARD, row["wage_type"], row["wage_code"]
                if not name or not code:
                    raise MappingError(f"{path.name} line {line}: wage_type and wage_code are required")
                key = (molga, normalise_name(name))
                if key in entries and entries[key] != code:
                    raise MappingError(f"{path.name} line {line}: '{name}' (molga {molga}) is mapped to both "
                                       f"{entries[key]} and {code}")
                entries[key] = code
                other = names_by_code.setdefault((molga, code), name)
                if normalise_name(other) != key[1]:
                    issues.append(f"Mapping assigns code {code} (molga {molga}) to both '{other}' and '{name}'")
        return cls(entries, issues)

    def lookup(self, molga: str, wage_type: str) -> str | None:
        k = normalise_name(wage_type)
        return self._entries.get((molga, k)) or self._entries.get((WILDCARD, k))

    def reserved_codes(self, molga: str) -> set[str]:
        """Every code the mapping has assigned for this molga (including wildcard rows)."""
        return {code for (m, _), code in self._entries.items() if m in (molga, WILDCARD)}


class WageCodeMapCache:
    """Re-reads the mapping only when the file changes, so edits apply without a restart."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._stamp: tuple[int, int] | None = None
        self._map: WageCodeMap | None = None

    def get(self) -> WageCodeMap:
        try:
            st = self.path.stat()
        except OSError:
            raise MappingError(f"Wage-code mapping file not found: {self.path}") from None
        stamp = (st.st_mtime_ns, st.st_size)
        with self._lock:
            if stamp != self._stamp or self._map is None:
                self._map = WageCodeMap.load(self.path)
                self._stamp = stamp
                log.info("Loaded wage-code mapping %s", self.path.name)
                for issue in self._map.issues:
                    log.warning(issue)
            return self._map


def assign_provisional(sub_category: str, taken: set[str]) -> str:
    """Next free multiple of STEP in the sub-category's range, above every code already taken in it."""
    start, end = CODE_RANGES.get(sub_category.upper(), FALLBACK_RANGE)
    in_range = [int(c) for c in taken if c.isdigit() and start <= int(c) < end]
    candidate = (max(in_range) // STEP + 1) * STEP if in_range else start
    while str(candidate) in taken:
        candidate += STEP
    if candidate >= end:
        raise MappingError(f"No free provisional wage codes left in {start}-{end - 1} for {sub_category}; "
                           "extend the mapping file")
    return str(candidate)
