# Copyright: Anki Software, LLC and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import csv
import io

import pytest

from webstudy.catalog import Catalog, InvalidImport, StalePreview


def test_preview_parses_bom_quotes_and_keeps_distinct_senses(tmp_path):
    catalog = Catalog(tmp_path / "catalog.sqlite3")
    preview = catalog.preview(
        b"\xef\xbb\xbfword\tmeaning\texample\r\n"
        b'bank\t"river\tshore"\t"an ""example"""\r\n'
        b"bank\tfinancial institution\t\r\n",
        "tsv",
    )

    assert (preview.added, preview.updated) == (2, 0)
    assert catalog.snapshot() == (0, [])
    assert catalog.confirm(preview.id, {}) == 1
    rows = list(csv.DictReader(io.StringIO(catalog.export_csv())))
    assert {(row["word"], row["meaning"], row["example"]) for row in rows} == {
        ("bank", "river\tshore", 'an "example"'),
        ("bank", "financial institution", ""),
    }
    assert len({row["id"] for row in rows}) == 2


def test_rename_keeps_id_and_duplicate_file_does_not_replay(tmp_path):
    path = tmp_path / "catalog.sqlite3"
    catalog = Catalog(path)
    original = b"id,word,meaning\nw001,recieve,old\nw002,keep,unchanged\n"
    first = catalog.preview(original, "csv")
    assert catalog.confirm(first.id, {}) == 1
    rename = catalog.preview(b"id,word,meaning\nw001,receive,fixed\n", "csv")
    assert rename.updated == 1
    assert catalog.confirm(rename.id, {}) == 2

    catalog = Catalog(path)
    assert catalog.confirm(first.id, {}) == 1
    duplicate = catalog.preview(original, "csv")
    assert duplicate.previous_version == 1
    assert catalog.confirm(duplicate.id, {}) == 1
    assert catalog.export_csv().splitlines() == [
        "id,word,meaning,example",
        "w001,receive,fixed,",
        "w002,keep,unchanged,",
    ]
    assert catalog.snapshot()[0] == 2


def test_homographs_require_explicit_choices_for_each_row(tmp_path):
    catalog = Catalog(tmp_path / "catalog.sqlite3")
    first = catalog.preview(b"id,word,meaning\nr,bank,shore\nf,bank,money\n", "csv")
    catalog.confirm(first.id, {})
    preview = catalog.preview(
        b"word,meaning\nbank,river shore\nbank,tilt\nbank,discard\n", "csv"
    )
    assert {row.line for row in preview.rows} == {2, 3, 4}
    assert all(
        {word.id for word in row.candidates} == {"r", "f"} for row in preview.rows
    )
    with pytest.raises(InvalidImport, match="Line 2"):
        catalog.confirm(preview.id, {})
    with pytest.raises(InvalidImport, match="Line 3"):
        catalog.confirm(preview.id, {2: "update:r", 3: "update:r", 4: "skip"})
    assert catalog.snapshot()[0] == 1
    assert catalog.confirm(preview.id, {2: "update:r", 3: "new", 4: "skip"}) == 2
    assert {word.meaning for word in catalog.snapshot()[1]} == {
        "money",
        "river shore",
        "tilt",
    }


def test_old_preview_cannot_overwrite_a_newer_confirmation(tmp_path):
    catalog = Catalog(tmp_path / "catalog.sqlite3")
    old = catalog.preview(b"id,word,meaning\nw001,word,old\n", "csv")
    new = catalog.preview(b"id,word,meaning\nw001,word,new\n", "csv")
    assert catalog.confirm(new.id, {}) == 1
    with pytest.raises(StalePreview):
        catalog.confirm(old.id, {})
    assert catalog.snapshot()[1][0].meaning == "new"


@pytest.mark.parametrize("with_id", [True, False])
def test_missing_example_column_preserves_the_existing_example(tmp_path, with_id):
    catalog = Catalog(tmp_path / "catalog.sqlite3")
    initial = catalog.preview(
        b"id,word,meaning,example\nw1,bank,old,on the bank\n", "csv"
    )
    catalog.confirm(initial.id, {})

    if with_id:
        update = catalog.preview(b"id,word,meaning\nw1,bank,river shore\n", "csv")
        assert update.rows[0].entry.example == "on the bank"
        choices = {}
    else:
        update = catalog.preview(b"word,meaning\nbank,river shore\n", "csv")
        choices = {2: "update:w1"}
    assert catalog.confirm(update.id, choices) == 2
    assert catalog.export_csv().splitlines()[1] == "w1,bank,river shore,on the bank"

    clear = catalog.preview(b"id,word,meaning,example\nw1,bank,river shore,\n", "csv")
    assert catalog.confirm(clear.id, {}) == 3
    assert catalog.export_csv().splitlines()[1] == "w1,bank,river shore,"


@pytest.mark.parametrize(
    ("data", "line"),
    [
        (b"word,meaning\nword, \n", 2),
        (b"id,word,meaning\nx,word,one\nx,other,two\n", 3),
        (b'word,meaning\n"x\nz",first\n"",second\n', 4),
        (b"word,meaning\nword,meaning,extra\n", 2),
        (b"word,word,meaning\nword,dup,meaning\n", 1),
    ],
)
def test_invalid_rows_report_physical_line_numbers(tmp_path, data, line):
    catalog = Catalog(tmp_path / "catalog.sqlite3")
    with pytest.raises(InvalidImport, match=rf"Line {line}\b"):
        catalog.preview(data, "csv")
    assert catalog.snapshot() == (0, [])
