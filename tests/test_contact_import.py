"""Reading a contact list out of a spreadsheet or a CSV.

The cases here are the ones real files have: a phone number Excel decided was
a quantity, a header row, a name with a comma in it, several sheets, a column
of something else entirely. Each one is a farmer who does or does not get
called, so each one is asserted rather than assumed.
"""

from __future__ import annotations

import io

import pytest
from openpyxl import Workbook

from api.services.contact_import import MAX_IMPORT_BYTES, extract_contacts, looks_like_number
from uaagro_domain.errors import ValidationError


def book(rows: list[list[object]], *, sheets: int = 1) -> bytes:
    """A real .xlsx file, written the way Excel writes one."""
    workbook = Workbook()
    for index in range(sheets):
        sheet = workbook.active if index == 0 else workbook.create_sheet()
        for row in rows:
            sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


# --------------------------------------------------------------------------- #
# Which cell is the number
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "cell",
    ["9876543210", "+91 98765 43210", "09876543210", "91-9876543210", "98765 43210"],
)
def test_a_mobile_number_is_recognised_however_it_is_written(cell: str) -> None:
    assert looks_like_number(cell)


@pytest.mark.parametrize(
    "cell",
    [
        "Ramesh Yadav",
        "1234567890",  # Indian mobiles do not start below 6.
        "98765",  # too short
        "9876543210987654",  # too long
        "NKSK-LKO-01",
        "",
    ],
)
def test_anything_that_is_not_a_mobile_number_is_left_alone(cell: str) -> None:
    assert not looks_like_number(cell)


# --------------------------------------------------------------------------- #
# Spreadsheets
# --------------------------------------------------------------------------- #


def test_a_number_stored_as_a_number_keeps_its_last_digit() -> None:
    """Excel stores 9876543210 as a float; naive reading gives "...0.0"."""
    data = book([["Ramesh Yadav", 9876543210]])
    found = extract_contacts(data, filename="list.xlsx")
    assert found.lines == ["Ramesh Yadav, 9876543210"]


def test_a_header_row_is_dropped_and_the_rows_under_it_are_not() -> None:
    data = book(
        [
            ["Farmer name", "Mobile", "Village"],
            ["Ramesh Yadav", "9876543210", "Mankapur"],
            ["Kamla Devi", "9876543213", "Sitapur"],
        ]
    )
    found = extract_contacts(data, filename="rabi.xlsx")
    assert found.lines == ["Ramesh Yadav, 9876543210", "Kamla Devi, 9876543213"]
    assert found.skipped == 1


def test_a_row_with_no_number_is_counted_not_guessed_at() -> None:
    data = book([["Total", "", ""], ["Kamla Devi", "9876543213"], ["", "", ""]])
    found = extract_contacts(data, filename="list.xlsx")
    assert found.lines == ["Kamla Devi, 9876543213"]
    assert found.skipped == 2


def test_a_name_with_a_comma_does_not_split_the_line() -> None:
    # The line is parsed as `name, number` downstream; a comma inside the name
    # would put half of it in the number field.
    data = book([["Yadav, Ramesh", "9876543210"]])
    found = extract_contacts(data, filename="list.xlsx")
    assert found.lines == ["Yadav Ramesh, 9876543210"]


def test_every_sheet_of_a_workbook_is_read() -> None:
    data = book([["Ramesh", "9876543210"]], sheets=3)
    found = extract_contacts(data, filename="districts.xlsx")
    assert found.sheets == 3
    assert len(found.lines) == 3


def test_a_number_with_no_name_comes_back_on_its_own() -> None:
    data = book([["9876543210"]])
    assert extract_contacts(data, filename="list.xlsx").lines == ["9876543210"]


# --------------------------------------------------------------------------- #
# Text files
# --------------------------------------------------------------------------- #


def test_a_csv_is_read_the_same_way() -> None:
    csv = b"Farmer,Mobile\nRamesh Yadav,9876543210\nKamla Devi,+91 98765 43213\n"
    found = extract_contacts(csv, filename="list.csv")
    assert found.lines == ["Ramesh Yadav, 9876543210", "Kamla Devi, +91 98765 43213"]


def test_a_semicolon_separated_export_is_read() -> None:
    # What an ERP in a locale that uses the comma as a decimal mark writes.
    data = "नाम;मोबाइल\nरमेश यादव;9876543210\n".encode()
    found = extract_contacts(data, filename="list.csv")
    assert found.lines == ["रमेश यादव, 9876543210"]


def test_a_plain_list_of_numbers_needs_no_columns() -> None:
    found = extract_contacts(b"9876543210\n9876543213\n", filename="numbers.txt")
    assert found.lines == ["9876543210", "9876543213"]


def test_a_byte_order_mark_does_not_become_part_of_the_first_name() -> None:
    # Excel's own "CSV UTF-8" export starts with one.
    found = extract_contacts("﻿Ramesh,9876543210\n".encode(), filename="list.csv")
    assert found.lines == ["Ramesh, 9876543210"]


# --------------------------------------------------------------------------- #
# What is refused, and how it says so
# --------------------------------------------------------------------------- #


def test_the_old_excel_format_says_what_to_do_about_it() -> None:
    with pytest.raises(ValidationError) as caught:
        extract_contacts(b"\xd0\xcf\x11\xe0" + b"\x00" * 100, filename="old.xls")
    assert "xlsx" in (caught.value.remedy or "").lower()


def test_an_unreadable_type_is_named() -> None:
    with pytest.raises(ValidationError) as caught:
        extract_contacts(b"%PDF-1.4", filename="list.pdf")
    assert "list.pdf" in caught.value.message


def test_a_broken_spreadsheet_is_refused_rather_than_half_read() -> None:
    with pytest.raises(ValidationError):
        extract_contacts(b"PK\x03\x04 not really a workbook", filename="list.xlsx")


def test_an_empty_file_is_refused() -> None:
    with pytest.raises(ValidationError):
        extract_contacts(b"", filename="list.csv")


def test_a_file_too_large_to_be_a_contact_list_is_refused() -> None:
    with pytest.raises(ValidationError) as caught:
        extract_contacts(b"x" * (MAX_IMPORT_BYTES + 1), filename="huge.csv")
    assert "MB" in (caught.value.remedy or "")
