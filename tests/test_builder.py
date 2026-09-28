"""Unit tests for the catalog rules, independent of HTTP."""

from decimal import Decimal
from pathlib import Path

import pytest

from app.catalog.builder import build_catalog, parse_amount
from app.catalog.reader import InputFormatError, Row, Table
from app.catalog.wage_codes import MappingError, WageCodeMap, assign_provisional

MASTER_COLS = ("payslip_id", "employee_id", "molga_country_grouping", "employment_group", "payroll_currency")
LINE_COLS = ("payslip_id", "employee_id", "section", "wage_type", "amount", "line_category", "applies_to")


def _table(name, cols, rows):
    return Table(name, cols, [Row(i, dict(zip(cols, r, strict=True))) for i, r in enumerate(rows, start=2)])


def _master(*rows):
    default = [("p1", "e1", "10", "G1", "USD"), ("p2", "e2", "10", "G2", "USD")]
    return _table("Master Data", MASTER_COLS, rows or default)


def _lines(*rows):
    return _table("Pay Line Items", LINE_COLS, rows)


CODES = WageCodeMap({("*", "regular salary"): "1000", ("*", "medical pre-tax"): "5100"})


@pytest.mark.parametrize("text,expected", [
    ("1234.56", Decimal("1234.56")), ("-1234.56", Decimal("-1234.56")), ("+5", Decimal("5")),
    ("1,234.56", Decimal("1234.56")), ("1,234,567", Decimal("1234567")),
    ("1234.56-", Decimal("-1234.56")), ("(1234.56)", Decimal("-1234.56")), ("0", Decimal("0")),
    ("1234,56", None), ("1.234,56", None), ("1,234", None), ("", None), ("12a", None), ("1e5", None),
    ("NaN", None), ("--5", None),
])
def test_parse_amount(text, expected):
    assert parse_amount(text) == expected


def test_totals_and_half_up_average():
    result = build_catalog(_master(), _lines(
        ("p1", "e1", "DEDUCTION", "Medical Pre-Tax", "123.225", "PRE_TAX_DEDUCTION", "CURRENT"),
        ("p2", "e2", "DEDUCTION", "Medical Pre-Tax", "123.225", "PRE_TAX_DEDUCTION", "CURRENT"),
    ), CODES)
    (row,) = result.rows
    assert (row.total_pay, row.average_pay, row.no_of_occurrence) == (Decimal("246.45"), Decimal("123.23"), 2)
    assert row.employment_groups == "G1; G2"
    assert row.category == "Deduction"


def test_reversals_net_off():
    result = build_catalog(_master(), _lines(
        ("p1", "e1", "EARNING", "Regular Salary", "100.00", "EARNING", "CURRENT"),
        ("p2", "e2", "EARNING", "Regular Salary", "100.00-", "EARNING", "CURRENT"),
    ), CODES)
    assert result.rows[0].total_pay == Decimal("0.00")


def test_positions_follow_code_order_per_molga():
    master = _master(("p1", "e1", "10", "G", "USD"), ("p2", "e2", "15", "G", "CAD"))
    result = build_catalog(master, _lines(
        ("p1", "e1", "DEDUCTION", "Medical Pre-Tax", "1", "PRE_TAX_DEDUCTION", ""),
        ("p1", "e1", "EARNING", "Regular Salary", "1", "EARNING", ""),
        ("p2", "e2", "EARNING", "Regular Salary", "1", "EARNING", ""),
    ), CODES)
    assert [(r.molga, r.wage_code, r.position) for r in result.rows] == [
        ("10", "1000", 1), ("10", "5100", 2), ("15", "1000", 1)]
    assert result.currencies == {"10": "USD", "15": "CAD"}


def test_join_is_strict_on_payslip_and_employee():
    with pytest.raises(InputFormatError) as e:
        build_catalog(_master(), _lines(("p1", "WRONG", "EARNING", "Regular Salary", "1", "EARNING", "")), CODES)
    assert "does not match" in e.value.errors[0]


def test_duplicate_payslip_in_master_is_an_error():
    with pytest.raises(InputFormatError) as e:
        build_catalog(_master(("p1", "e1", "10", "G", "USD"), ("p1", "e1", "10", "G", "USD")),
                      _lines(("p1", "e1", "EARNING", "Regular Salary", "1", "EARNING", "")), CODES)
    assert "duplicate payslip_id" in e.value.errors[0]


def test_error_list_is_capped():
    rows = [("nope", "e", "EARNING", "X", "1", "EARNING", "")] * 100
    with pytest.raises(InputFormatError) as e:
        build_catalog(_master(), _lines(*rows), CODES)
    assert len(e.value.errors) == 26 and e.value.errors[-1].startswith("...")


def test_provisional_codes_skip_reserved_and_each_other():
    taken = {"1000", "1100", "1110"}
    first = assign_provisional("EARNING", taken)
    taken.add(first)
    assert (first, assign_provisional("EARNING", taken)) == ("1120", "1130")
    assert assign_provisional("SOMETHING_NEW", set()) == "9000"


def test_mapping_rejects_conflicts_and_bad_headers(tmp_path: Path):
    f = tmp_path / "m.csv"
    f.write_text("molga,wage_type,wage_code\n*,A,1\n*,a,2\n")
    with pytest.raises(MappingError, match="mapped to both"):
        WageCodeMap.load(f)
    f.write_text("name,code\nA,1\n")
    with pytest.raises(MappingError, match="must have columns"):
        WageCodeMap.load(f)
    f.write_text("molga,wage_type,wage_code\n*,A,1\n*,B,1\n10,A,7\n")
    m = WageCodeMap.load(f)
    assert m.issues and m.lookup("10", " a ") == "7" and m.lookup("99", "A") == "1"


# ---------- wage_code column (current template) ----------

CODED_COLS = (*LINE_COLS, "wage_code")


def _coded(*rows):
    return _table("Pay Line Items", CODED_COLS, rows)


def test_groups_by_input_code_and_ignores_mapping():
    result = build_catalog(_master(), _coded(
        ("p1", "e1", "EARNING", "Regular Salary", "100", "EARNING", "CURRENT", "0999"),
        ("p2", "e2", "EARNING", "Regular Salary", "50", "EARNING", "CURRENT", "0999"),
    ), CODES)
    (row,) = result.rows
    assert (row.wage_code, row.no_of_occurrence, row.total_pay) == ("0999", 2, Decimal("100") + Decimal("50"))
    assert result.code_sources == {"input": 2, "mapping": 0, "provisional": 0}


def test_code_conflict_names_both_rows():
    with pytest.raises(InputFormatError) as e:
        build_catalog(_master(), _coded(
            ("p1", "e1", "EARNING", "Regular Salary", "1", "EARNING", "", "1000"),
            ("p2", "e2", "EARNING", "Holiday Pay", "1", "EARNING", "", "1000"),
        ), CODES)
    assert e.value.errors == ["Pay Line Items row 3: wage_code '1000' (molga 10) is used for 'Holiday Pay' but also "
                              "for 'Regular Salary' (row 2); one code must mean one wage type"]


def test_same_code_in_different_molgas_may_name_different_wage_types():
    master = _master(("p1", "e1", "10", "G", "USD"), ("p2", "e2", "15", "G", "CAD"))
    result = build_catalog(master, _coded(
        ("p1", "e1", "EARNING", "Regular Salary", "1", "EARNING", "", "1000"),
        ("p2", "e2", "EARNING", "Salaire de base", "1", "EARNING", "", "1000"),
    ), CODES)
    names = [(r.molga, r.wage_type_local_lang) for r in result.rows]
    assert names == [("10", "Regular Salary"), ("15", "Salaire de base")]


def test_mapping_issues_are_only_reported_when_the_mapping_is_used():
    noisy = WageCodeMap({("*", "regular salary"): "1000"}, issues=["Mapping assigns code 1 to both A and B"])
    line = ("p1", "e1", "EARNING", "Regular Salary", "1", "EARNING", "")
    coded = build_catalog(_master(), _coded((*line, "1000")), noisy)
    legacy = build_catalog(_master(), _lines(line), noisy)
    assert coded.warnings == [] and legacy.warnings == noisy.issues
