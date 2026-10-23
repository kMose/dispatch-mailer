"""{Salutation} and spreadsheet whitespace cleaning."""
from __future__ import annotations

from dispatch import db
from dispatch.engine import preflight
from dispatch.mail import blocks, compose
from dispatch.sheet.reader import normalize_salutation, read_sheet


def test_salutation_header_is_cleaned_and_canonicalised(tmp_path):
    from tests.conftest import write_workbook

    path = write_workbook(
        tmp_path / "leads.xlsx",
        [{"email": "a@example.com", "Salutation": "  MR  ", "first_name": " Mary   Ann ",
          "last_name": " O'Neil\u00a0", "company": " Acme   Ltd "}],
        headers=["Salutation", "FirstName", "LastName", "Company", "Email"],
    )
    contact = read_sheet(path).contacts[0]

    assert contact.extra["Salutation"] == "Mr."
    assert contact.first_name == "Mary Ann"
    assert contact.last_name == "O'Neil"
    assert contact.company == "Acme Ltd"
    assert contact.full_name == "Mary Ann O'Neil"
    assert contact.as_template_values()["Salutation"] == "Mr."


def test_honorific_header_maps_to_salutation(tmp_path):
    from tests.conftest import write_workbook

    path = write_workbook(
        tmp_path / "leads.xlsx",
        [{"email": "a@example.com", "Honorific": "dr"}],
        headers=["Honorific", "Email"],
    )
    contact = read_sheet(path).contacts[0]
    assert contact.extra["Salutation"] == "Dr."


def test_normalize_salutation_variants():
    assert normalize_salutation("mr") == "Mr."
    assert normalize_salutation("MRS.") == "Mrs."
    assert normalize_salutation(" Ms ") == "Ms."
    assert normalize_salutation("Mx") == "Mx."
    assert normalize_salutation("Professor") == "Professor"  # unknown: left alone


def test_salutation_reaches_db_rows_and_the_renderer(tmp_path):
    from tests.conftest import HEADERS, make_config

    headers = HEADERS + ["Salutation"]
    cfg = make_config(tmp_path, [{
        "email": "a@example.com",
        "first_name": "Mary  Ann",
        "last_name": " Smith ",
        "Salutation": "ms",
    }], headers=headers, dry_run=True)

    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    preflight.import_sheet(cfg, conn)
    row = db.contact_by_email(conn, "a@example.com")
    values = compose.values_for_db_row(cfg, row)

    assert values["Salutation"] == "Ms."
    assert compose.render_subject(cfg, values, "Dear {Salutation} {LastName},") == \
        "Dear Ms. Smith,"

    # And the contact-level path used by the builder preview.
    contact = read_sheet(cfg.sheet_file, cfg.campaign.sheet_tab).contacts[0]
    contact_values = compose.template_values(cfg, contact)
    assert contact_values["Salutation"] == "Ms."
    conn.close()


def test_salutation_is_a_known_placeholder():
    doc = blocks.new_document("t")
    doc["blocks"] = [{"type": "paragraph",
                      "text": "Dear {Salutation} {LastName}, this is a useful note."}]
    errors, _ = blocks.validate_document(doc, [])
    assert not any("Salutation" in error for error in errors)
