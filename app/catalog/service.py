"""Synchronous catalog generation (runs in a worker thread): read -> validate/aggregate -> write."""

from dataclasses import dataclass
from pathlib import Path

from app.catalog.builder import CatalogResult, build_catalog
from app.catalog.reader import read_csv, read_xlsx
from app.catalog.wage_codes import WageCodeMap
from app.catalog.writer import write_catalog


@dataclass(frozen=True)
class CatalogInput:
    kind: str                 # "xlsx" | "csv"
    primary: Path             # workbook, or Pay Line Items CSV
    master: Path | None = None


def generate_catalog(inp: CatalogInput, output: Path, codes: WageCodeMap) -> CatalogResult:
    if inp.kind == "xlsx":
        master, lines = read_xlsx(inp.primary)
    else:
        master, lines = read_csv(inp.primary, inp.master)
    result = build_catalog(master, lines, codes)
    write_catalog(result, output)
    return result
