"""Aggregates payslip line items into the wage-type catalog: one row per wage code per molga.

Where each line's wage code comes from, in order:
  1. the line's own `wage_code` column (current template; authoritative),
  2. the maintained mapping file, by wage-type name (legacy files without the column, or blank cells),
  3. a provisional code, flagged in the output and the warnings.

Deliberately strict: payroll figures that can't be parsed unambiguously, joined reliably, summed
meaningfully (mixed currencies) or attributed to one wage type (a code used for two different
names) are rejected with row-level errors rather than guessed at.
"""

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from app.catalog.reader import InputFormatError, Row, Table
from app.catalog.wage_codes import WageCodeMap, assign_provisional, normalise_name

SECTION_TO_CATEGORY = {
    "EARNING": "Earning",
    "DEDUCTION": "Deduction",
    "ER_CONTRIB": "Employer Contribution",
}
CURRENT = "CURRENT"
CENT = Decimal("0.01")
MAX_REPORTED_ERRORS = 25

# Demo codes are numeric ("1000"); SAP wage types can also be alphanumeric ("M100") or technical ("/101").
_WAGE_CODE = re.compile(r"^[A-Za-z0-9/_.-]{1,10}$")
_PLAIN_NUMBER = re.compile(r"^\d+(\.\d+)?$")
# Thousands separators are only accepted when unambiguous: "1,234.56" or "1,234,567" — never a bare "1,234",
# which is 1234 in en-US but 1.234 in much of Europe.
_GROUPED_NUMBER = re.compile(r"^\d{1,3}(,\d{3})+\.\d+$|^\d{1,3}(,\d{3}){2,}$")


def parse_amount(text: str) -> Decimal | None:
    """Parses '1234.56', '-1234.56', '1,234.56', SAP trailing-minus '1234.56-' and accounting '(1234.56)'.
    Returns None for anything ambiguous or malformed (e.g. decimal-comma '1234,56')."""
    t = text.strip().replace(" ", "")
    negative = False
    if t.startswith("(") and t.endswith(")"):
        t, negative = t[1:-1], True
    elif t.endswith("-"):
        t, negative = t[:-1], True
    elif t and t[0] in "+-":
        t, negative = t[1:], t[0] == "-"
    if _PLAIN_NUMBER.match(t):
        value = Decimal(t)
    elif _GROUPED_NUMBER.match(t):
        value = Decimal(t.replace(",", ""))
    else:
        return None
    return -value if negative else value


@dataclass
class CatalogRow:
    wage_type_local_lang: str
    wage_code: str
    no_of_occurrence: int
    total_pay: Decimal
    average_pay: Decimal
    category: str
    sub_category: str
    position: int
    employment_groups: str
    molga: str
    provisional_code: bool = False


@dataclass
class CatalogResult:
    rows: list[CatalogRow]
    warnings: list[str] = field(default_factory=list)
    line_count: int = 0
    payslip_count: int = 0
    currencies: dict[str, str] = field(default_factory=dict)  # molga -> currency, when known
    code_sources: dict[str, int] = field(default_factory=dict)  # line counts: input / mapping / provisional

    @property
    def provisional_count(self) -> int:
        return sum(r.provisional_code for r in self.rows)


@dataclass
class _Line:
    row: Row
    molga: str
    wage_type: str
    section: str
    sub_category: str
    amount: Decimal
    employment_group: str
    code: str | None       # None until a provisional code is assigned
    source: str            # "input" | "mapping" | "provisional"


@dataclass
class _Acc:
    wage_type: str          # first spelling seen; one code may only ever carry one (normalised) name
    name_key: str
    first_row: int
    count: int = 0
    total: Decimal = Decimal(0)
    sections: Counter = field(default_factory=Counter)
    sub_categories: Counter = field(default_factory=Counter)
    groups: set = field(default_factory=set)
    provisional: bool = False


class _Errors(list):
    def add(self, table: Table, row: Row, msg: str) -> None:
        if len(self) < MAX_REPORTED_ERRORS:
            self.append(f"{table.name} row {row.number}: {msg}")
        elif len(self) == MAX_REPORTED_ERRORS:
            self.append("... further errors omitted")


def _index_master(master: Table, errors: _Errors) -> dict[str, Row]:
    by_payslip: dict[str, Row] = {}
    for r in master.rows:
        pid = r["payslip_id"]
        if not pid:
            errors.add(master, r, "payslip_id is empty")
        elif pid in by_payslip:
            errors.add(master, r, f"duplicate payslip_id '{pid}' (first seen on row {by_payslip[pid].number})")
        elif not r["molga_country_grouping"]:
            errors.add(master, r, "molga_country_grouping is empty")
        else:
            by_payslip[pid] = r
    return by_payslip


def build_catalog(master: Table | None, lines: Table, codes: WageCodeMap) -> CatalogResult:
    errors = _Errors()
    has_code_column = lines.has("wage_code")
    # Mapping integrity only matters when the mapping is actually consulted.
    warnings: list[str] = [] if has_code_column else list(codes.issues)
    by_payslip = _index_master(master, errors) if master is not None else {}

    parsed: list[_Line] = []
    payslips: set[str] = set()
    currencies: dict[str, set[str]] = defaultdict(set)
    non_current: Counter = Counter()

    for r in lines.rows:
        if master is None:
            m = r
            if not r["molga_country_grouping"]:
                errors.add(lines, r, "molga_country_grouping is empty")
                continue
        else:
            m = by_payslip.get(r["payslip_id"])
            if m is None:
                errors.add(lines, r, f"payslip_id '{r['payslip_id']}' not found in {master.name}")
                continue
            if r["employee_id"] and m["employee_id"] and r["employee_id"] != m["employee_id"]:
                errors.add(lines, r, f"employee_id '{r['employee_id']}' does not match {master.name} "
                                     f"('{m['employee_id']}') for payslip '{r['payslip_id']}'")
                continue

        wage_type = " ".join(r["wage_type"].split())
        section = r["section"].upper()
        amount = parse_amount(r["amount"])
        if not wage_type:
            errors.add(lines, r, "wage_type is empty")
            continue
        if section not in SECTION_TO_CATEGORY:
            errors.add(lines, r, f"unknown section '{r['section']}' (expected {', '.join(SECTION_TO_CATEGORY)})")
            continue
        if amount is None:
            errors.add(lines, r, f"amount '{r['amount']}' is not an unambiguous number "
                                 "(use a dot as the decimal separator, e.g. 1234.56)")
            continue

        raw_code = r["wage_code"]
        if raw_code and not _WAGE_CODE.match(raw_code):
            errors.add(lines, r, f"wage_code '{raw_code}' is not a valid code "
                                 "(1-10 characters: letters, digits, / _ . -)")
            continue

        molga = m["molga_country_grouping"]
        if currency := (m["payroll_currency"] or r["payroll_currency"]).upper():
            currencies[molga].add(currency)
        if (applies := r["applies_to"].upper()) and applies != CURRENT:
            non_current[applies] += 1

        if raw_code:
            code, source = raw_code, "input"
        elif mapped := codes.lookup(molga, wage_type):
            code, source = mapped, "mapping"
        else:
            code, source = None, "provisional"
        parsed.append(_Line(r, molga, wage_type, section, r["line_category"].upper(), amount,
                            m["employment_group"], code, source))
        payslips.add(r["payslip_id"])

    for molga, found in sorted(currencies.items()):
        if len(found) > 1:
            errors.append(f"molga {molga} mixes payroll currencies ({', '.join(sorted(found))}); "
                          "amounts in different currencies cannot be summed — split the file by currency")
    if errors:
        raise InputFormatError(f"Input validation failed ({len(errors)} problem(s))", list(errors))

    _assign_provisional_codes(parsed, codes, warnings)
    accs = _aggregate(parsed, lines, errors)
    if errors:
        raise InputFormatError(f"Input validation failed ({len(errors)} problem(s))", list(errors))

    sources = Counter(line.source for line in parsed)
    if has_code_column and sources["mapping"]:
        warnings.append(f"{sources['mapping']} line(s) had an empty wage_code; their code was taken from the "
                        "wage-code mapping by wage-type name")
    if non_current:
        breakdown = ", ".join(f"{k}: {v}" for k, v in sorted(non_current.items()))
        warnings.append(f"{sum(non_current.values())} line(s) have applies_to other than CURRENT ({breakdown}); "
                        "they are included in the totals — confirm they should not be excluded")
    _warn_names_with_several_codes(accs, warnings)

    rows = [_to_row(key, acc, warnings) for key, acc in accs.items()]
    _assign_positions(rows)

    return CatalogResult(
        rows=rows, warnings=warnings, line_count=len(lines.rows), payslip_count=len(payslips),
        currencies={m: next(iter(c)) for m, c in currencies.items()},
        code_sources={k: sources[k] for k in ("input", "mapping", "provisional")},
    )


def _assign_provisional_codes(parsed: list[_Line], codes: WageCodeMap, warnings: list[str]) -> None:
    """One provisional code per (molga, wage-type name) that has no code from the input or the mapping.
    It avoids every code in use in the file and every code the mapping reserves for the molga."""
    unresolved = [line for line in parsed if line.code is None]
    if not unresolved:
        return
    taken: dict[str, set[str]] = {}
    for molga in {line.molga for line in unresolved}:
        taken[molga] = codes.reserved_codes(molga) | {line.code for line in parsed if line.molga == molga and line.code}
    assigned: dict[tuple[str, str], str] = {}
    for line in unresolved:
        key = (line.molga, normalise_name(line.wage_type))
        if key not in assigned:
            assigned[key] = assign_provisional(line.sub_category, taken[line.molga])
            taken[line.molga].add(assigned[key])
            warnings.append(f"'{line.wage_type}' (molga {line.molga}) has no wage_code and is not in the "
                            f"wage-code mapping; provisional code {assigned[key]} assigned — confirm the real code")
        line.code = assigned[key]


def _aggregate(parsed: list[_Line], lines: Table, errors: _Errors) -> dict[tuple[str, str], _Acc]:
    """Groups lines by (molga, wage_code): a multi-country file yields one catalog block per country grouping."""
    accs: dict[tuple[str, str], _Acc] = {}
    for line in parsed:
        name_key = normalise_name(line.wage_type)
        acc = accs.setdefault((line.molga, line.code), _Acc(line.wage_type, name_key, line.row.number))
        if name_key != acc.name_key:
            errors.add(lines, line.row, f"wage_code '{line.code}' (molga {line.molga}) is used for "
                                        f"'{line.wage_type}' but also for '{acc.wage_type}' (row {acc.first_row}); "
                                        "one code must mean one wage type")
            continue
        acc.count += 1
        acc.total += line.amount
        acc.sections[line.section] += 1
        acc.sub_categories[line.sub_category] += 1
        if line.employment_group:
            acc.groups.add(line.employment_group)
        acc.provisional |= line.source == "provisional"
    return accs


def _warn_names_with_several_codes(accs: dict[tuple[str, str], _Acc], warnings: list[str]) -> None:
    codes_by_name: dict[tuple[str, str], list[str]] = defaultdict(list)
    for (molga, code), acc in accs.items():
        codes_by_name[(molga, acc.name_key)].append(code)
    for (molga, _), found in sorted(codes_by_name.items()):
        if len(found) > 1:
            warnings.append(f"'{accs[(molga, found[0])].wage_type}' (molga {molga}) appears under several wage codes "
                            f"({', '.join(sorted(found, key=_code_order))}); each code is a separate catalog row")


def _to_row(key: tuple[str, str], acc: _Acc, warnings: list[str]) -> CatalogRow:
    molga, code = key
    if len(acc.sections) > 1 or len(acc.sub_categories) > 1:
        warnings.append(f"wage_code {code} '{acc.wage_type}' appears under several sections/categories "
                        f"({dict(acc.sections)} / {dict(acc.sub_categories)}); the most frequent is used")
    return CatalogRow(
        wage_type_local_lang=acc.wage_type,
        wage_code=code,
        no_of_occurrence=acc.count,
        total_pay=acc.total.quantize(CENT, ROUND_HALF_UP),
        # Averaged from the unrounded total, then rounded half-up (123.225 -> 123.23, as in the reference output).
        average_pay=(acc.total / acc.count).quantize(CENT, ROUND_HALF_UP),
        category=SECTION_TO_CATEGORY[acc.sections.most_common(1)[0][0]],
        sub_category=acc.sub_categories.most_common(1)[0][0],
        position=0,
        employment_groups="; ".join(sorted(acc.groups)),
        molga=molga,
        provisional_code=acc.provisional,
    )


def _code_order(code: str) -> tuple:
    return (0, int(code), "") if code.isdigit() else (1, 0, code)  # numeric codes first, then e.g. SAP "/101"


def _assign_positions(rows: list[CatalogRow]) -> None:
    rows.sort(key=lambda x: (x.molga, _code_order(x.wage_code)))
    position, molga = 0, None
    for row in rows:
        position = 1 if row.molga != molga else position + 1
        molga = row.molga
        row.position = position
