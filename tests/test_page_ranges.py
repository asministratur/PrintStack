"""Unit tests for page range parsing and skipped-page exclusion."""

import pytest

from printstack.print_engine import parse_page_selection


def test_all_odd_even():
    assert parse_page_selection(6, "all") == [0, 1, 2, 3, 4, 5]
    assert parse_page_selection(6, "odd") == [0, 2, 4]
    assert parse_page_selection(6, "even") == [1, 3, 5]
    assert parse_page_selection(0, "all") == []


def test_custom_ranges_and_dedup():
    assert parse_page_selection(10, "custom", "1-3, 5, 3-2, 9-12") == [0, 1, 2, 4, 8, 9]
    assert parse_page_selection(4, "custom", "4-2") == [3, 2, 1]
    assert parse_page_selection(4, "custom", "") == [0, 1, 2, 3]


def test_skipped_pages_subtraction():
    assert parse_page_selection(5, "all", skipped_pages=[2, 4]) == [0, 2, 4]
    assert parse_page_selection(6, "custom", "1-5", skipped_pages={1, 3, 5}) == [1, 3]


@pytest.mark.parametrize("bad_expr", ["abc", "1--3", "-3", "3-", "0", "1, , 2"])
def test_invalid_custom_ranges_raise(bad_expr: str):
    with pytest.raises(ValueError):
        parse_page_selection(5, "custom", bad_expr)
