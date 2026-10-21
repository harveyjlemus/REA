import csv

import pytest

from lib.columns import apply_overrides, detect_mapping, read_table


def write(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerows(rows)


def test_apollo_with_mv_columns(cfg, tmp_path):
    p = tmp_path / "apollo.csv"
    write(p, [["First Name", "Last Name", "Full Name", "Title", "Seniority", "Email", "quality", "result", "free", "role",
               "Company Name", "Company Website Short", "Lead City", "Lead State", "Company SEO Description"],
              ["Ann", "Lee", "Ann Lee", "Owner", "owner", "ann@lee.test", "good", "ok", "no", "no", "Lee Roofing",
               "lee.test", "Salem", "MA", "line one\nline two, with a comma"]])
    header, rows = read_table(p)
    assert len(rows) == 1, "a multi-line cell must not split the row"
    m = detect_mapping(header, cfg)
    assert m.format_id == "apollo" and m.has_mv_columns
    assert m.fields["website"] == "Company Website Short"
    assert m.fields["mv_result"] == "result" and m.fields["email"] == "Email"


def test_scrapio_name_is_the_company(cfg, tmp_path):
    p = tmp_path / "scrap.csv"
    write(p, [["Export - Scrap.io"],
              ["Name", "Main type", "Google ID", "Website", "Website (root url)", "Email", "Individual email",
               "Individual email first name", "Individual email last name", "Is closed permanently", "All emails"],
              ["Acme Roofing", "Roofer", "0x1", "http://acme.test/", "http://acme.test", "info@acme.test",
               "mike@acme.test", "Mike", "", "No", "[]"]])
    header, rows = read_table(p)
    m = detect_mapping(header, cfg)
    assert m.format_id == "scrapio"
    assert m.fields["company"] == "Name"
    assert m.fields["email"] == "Individual email"
    assert m.fields["business_email"] == "Email"
    assert m.fields["first_name"] == "Individual email first name"


def test_scrapio_xlsx_with_a_title_row(cfg, tmp_path):
    from openpyxl import Workbook
    from lib.state import RunDir
    wb = Workbook()
    ws = wb.active
    ws.append(["Export - Scrap.io"])
    ws.append(["Name", "Main type", "Google ID", "Website", "Website (root url)", "Email", "Is closed permanently"])
    ws.append(["Acme Roofing", "Roofer", "0x1", "http://acme.test/", "http://acme.test", "info@acme.test", "No"])
    p = tmp_path / "scrap.xlsx"
    wb.save(p)
    header, rows = read_table(p)
    assert header[0] == "Name" and rows[0][0] == "Acme Roofing"
    assert detect_mapping(header, cfg).format_id == "scrapio"
    mapping, run_rows = RunDir(tmp_path / "run").create(p, cfg)
    assert run_rows[0].input["company"] == "Acme Roofing"
    assert (tmp_path / "run" / "input.csv").is_file()


def test_generic_name_column_and_overrides(cfg, tmp_path):
    p = tmp_path / "g.csv"
    write(p, [["Company", "Name", "Site"], ["Acme", "Mike Rivera", "acme.test"]])
    header, _ = read_table(p)
    m = detect_mapping(header, cfg)
    assert m.fields["full_name"] == "Name"
    assert m.fields["website"] == "Site"
    m = apply_overrides(m, header, ["website=Site", "title="])
    with pytest.raises(ValueError):
        apply_overrides(m, header, ["nonsense=Site"])
