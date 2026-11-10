"""The editions gate: shelve seals, verify catches tampering, and a
receipt is written for everything shelved."""

import hashlib
import json

import pytest

from passband import editions


@pytest.fixture
def src(tmp_path):
    directory = tmp_path / "payload"
    directory.mkdir()
    (directory / "tle.txt").write_text("fake elements\n", encoding="utf-8")
    (directory / "notes.md").write_text("read me\n", encoding="utf-8")
    return directory


def shelve_it(src, data_dir):
    return editions.shelve(
        src, data_dir, name="tle", kind="reference", version="2026-11",
        source="test fixture", need="pass prediction needs elements",
        edition_date="2026-11-09",
    )


def test_shelve_writes_manifest_and_receipt(src, data_dir):
    target = shelve_it(src, data_dir)
    manifest = editions.load_manifest(target)
    assert manifest["name"] == "tle"
    assert manifest["kind"] == "reference"
    assert manifest["date"] == "2026-11-09"
    assert set(manifest["files"]) == {"tle.txt", "notes.md"}
    receipt_path = editions.receipts_dir(data_dir) / "tle@2026-11.json"
    receipt = json.loads(receipt_path.read_text())
    assert receipt["need"] == "pass prediction needs elements"
    assert receipt["made_from"] == str(src)
    expected = hashlib.sha256(
        (target / "manifest.json").read_bytes()
    ).hexdigest()
    assert receipt["hash"] == expected


def test_verify_passes_on_fresh_edition(src, data_dir):
    target = shelve_it(src, data_dir)
    ok, reasons = editions.verify(target)
    assert ok, reasons


def test_verify_refuses_tampered_file(src, data_dir):
    target = shelve_it(src, data_dir)
    (target / "tle.txt").write_text("forged elements\n", encoding="utf-8")
    ok, reasons = editions.verify(target)
    assert not ok
    assert any("hash mismatch" in reason for reason in reasons)


def test_verify_refuses_missing_and_unsealed_files(src, data_dir):
    target = shelve_it(src, data_dir)
    (target / "notes.md").unlink()
    ok, reasons = editions.verify(target)
    assert not ok and any("missing" in reason for reason in reasons)
    target2 = editions.shelve(
        src, data_dir, name="tle2", kind="reference", version="1",
        source="t", need="n",
    )
    (target2 / "sneaked-in.txt").write_text("x", encoding="utf-8")
    ok, reasons = editions.verify(target2)
    assert not ok and any("unsealed" in reason for reason in reasons)


def test_verify_named_stamp_and_fallback(src, data_dir):
    assert editions.verify_named(data_dir, "tle", kind="reference") == ("system", True, [])
    shelve_it(src, data_dir)
    stamp, ok, _ = editions.verify_named(data_dir, "tle", kind="reference")
    assert stamp == "tle@2026-11"
    assert ok


def test_list_editions(src, data_dir):
    shelve_it(src, data_dir)
    found = editions.list_editions(data_dir)
    assert len(found) == 1
    assert found[0]["ok"] is True
    assert found[0]["files"] == 2


def test_bad_kind_and_empty_source_refused(data_dir, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ValueError):
        editions.shelve(empty, data_dir, name="x", kind="reference",
                        version="1", source="t", need="n")
    with pytest.raises(ValueError):
        editions.shelve(empty, data_dir, name="x", kind="weird",
                        version="1", source="t", need="n")
