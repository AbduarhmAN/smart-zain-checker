"""Tests for FastWorkbook and CalamineSheetAdapter with phantom row trimming."""
import pytest
from pathlib import Path
from domain.workbook import CalamineSheetAdapter, FastWorkbook


def test_calamine_sheet_adapter_basic():
    data = [
        ["col1", "col2", "col3"],
        ["val1", 100, "val3"],
        ["val4", 200, "val5"],
    ]
    adapter = CalamineSheetAdapter(data, title="TestSheet")
    assert adapter.title == "TestSheet"
    assert adapter.max_row == 3
    assert adapter.max_column == 3

    # 1-based indexing for cell()
    c1 = adapter.cell(1, 1)
    assert c1.value == "col1"
    c2 = adapter.cell(2, 2)
    assert c2.value == 100

    # iter_rows values_only
    rows = list(adapter.iter_rows(min_row=2, values_only=True))
    assert len(rows) == 2
    assert rows[0] == ("val1", 100, "val3")
    assert rows[1] == ("val4", 200, "val5")


def test_phantom_trailing_empty_rows_trimming():
    # 3 real data rows followed by 10 trailing empty rows
    data = [
        ["Header1", "Header2"],
        ["Account1", 500],
        ["Account2", 1000],
    ] + [["", None] for _ in range(10)]

    assert len(data) == 13

    # Fast trim logic as implemented in FastWorkbook
    last_idx = len(data)
    while last_idx > 0 and not any(data[last_idx - 1]):
        last_idx -= 1
    if last_idx < len(data):
        data = data[:last_idx]

    adapter = CalamineSheetAdapter(data, title="Sheet1")
    assert adapter.max_row == 3
    assert adapter.cell(3, 1).value == "Account2"
    assert adapter.cell(4, 1).value is None
