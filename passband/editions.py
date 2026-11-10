"""The editions gate.

An edition is a directory of files plus a manifest that seals them:
manifest.json names the edition (name, kind, version, date, source)
and carries the sha256 of every file it contains. Capability in Passband
- a decoder, reference data like orbital elements or an aircraft
database, a day plan - is shelved as an edition before it runs with
authority, and verified against its manifest every time the gate is
asked.

Shelving also writes a receipt under <data_dir>/receipts/: the need
that called the edition into being, what it was made from, and the
hash it answers to (the sha256 of the manifest itself). Trust here is
by reproduction of hashes, not by signature and not by network: an
edition that travelled by wire and one carried by hand pass the same
gate.

This module is not a package manager. It downloads nothing, installs
nothing, and never touches a running system. It seals directories and
checks seals.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import date
from pathlib import Path

from .events import now_iso

KINDS = ("decoder", "reference", "plan")
MANIFEST_NAME = "manifest.json"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def editions_dir(data_dir) -> Path:
    return Path(data_dir) / "editions"


def receipts_dir(data_dir) -> Path:
    return Path(data_dir) / "receipts"


def edition_path(data_dir, name: str, version: str) -> Path:
    return editions_dir(data_dir) / f"{name}@{version}"


def _collect_files(root: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name != MANIFEST_NAME:
            files[path.relative_to(root).as_posix()] = _sha256_file(path)
    return files


def shelve(
    source_dir,
    data_dir,
    name: str,
    kind: str,
    version: str,
    source: str,
    need: str,
    edition_date: str | None = None,
) -> Path:
    """Seal the files under source_dir as an edition and write a receipt.

    Copies the source files into <data_dir>/editions/<name>@<version>,
    writes manifest.json with every file's sha256, and writes the
    receipt under <data_dir>/receipts/. Returns the edition directory.
    Raises ValueError on a bad kind or an empty source.
    """
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")
    source_path = Path(source_dir)
    if not source_path.is_dir():
        raise ValueError(f"source is not a directory: {source_dir}")
    staged = [p for p in source_path.rglob("*") if p.is_file() and p.name != MANIFEST_NAME]
    if not staged:
        raise ValueError(f"source directory holds no files: {source_dir}")

    target = edition_path(data_dir, name, version)
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    for path in staged:
        dest = target / path.relative_to(source_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)

    manifest = {
        "name": name,
        "kind": kind,
        "version": version,
        "date": edition_date or date.today().isoformat(),
        "source": source,
        "files": _collect_files(target),
    }
    manifest_text = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    (target / MANIFEST_NAME).write_text(manifest_text, encoding="utf-8")
    manifest_hash = hashlib.sha256(manifest_text.encode("utf-8")).hexdigest()

    receipt = {
        "name": name,
        "kind": kind,
        "version": version,
        "need": need,
        "made_from": str(source_path),
        "hash": manifest_hash,
        "edition_dir": str(target),
        "shelved_at": now_iso(),
    }
    rdir = receipts_dir(data_dir)
    rdir.mkdir(parents=True, exist_ok=True)
    (rdir / f"{name}@{version}.json").write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return target


def load_manifest(edition_dir) -> dict:
    path = Path(edition_dir) / MANIFEST_NAME
    if not path.is_file():
        raise ValueError(f"no manifest.json in edition directory: {edition_dir}")
    return json.loads(path.read_text(encoding="utf-8"))


def verify(edition_dir) -> tuple[bool, list[str]]:
    """Rehash an edition against its manifest.

    Returns (True, []) only when the manifest parses, names a known
    kind, and every sealed file is present with a matching hash and
    no unsealed file has crept in.
    """
    root = Path(edition_dir)
    if not root.is_dir():
        return False, [f"edition directory does not exist: {edition_dir}"]
    try:
        manifest = load_manifest(root)
    except (ValueError, json.JSONDecodeError) as exc:
        return False, [f"manifest unreadable: {exc}"]
    reasons: list[str] = []
    if manifest.get("kind") not in KINDS:
        reasons.append(f"manifest kind is not one of {KINDS}: {manifest.get('kind')!r}")
    sealed = manifest.get("files")
    if not isinstance(sealed, dict) or not sealed:
        reasons.append("manifest seals no files")
        return False, reasons
    present = _collect_files(root)
    for relpath, expected in sealed.items():
        actual = present.get(relpath)
        if actual is None:
            reasons.append(f"sealed file missing: {relpath}")
        elif actual != expected:
            reasons.append(f"hash mismatch: {relpath}")
    for relpath in present:
        if relpath not in sealed:
            reasons.append(f"unsealed file present: {relpath}")
    return (not reasons), reasons


def list_editions(data_dir) -> list[dict]:
    """All shelved editions with their manifest facts and seal state."""
    root = editions_dir(data_dir)
    found: list[dict] = []
    if not root.is_dir():
        return found
    for path in sorted(root.iterdir()):
        if not path.is_dir():
            continue
        try:
            manifest = load_manifest(path)
        except (ValueError, json.JSONDecodeError):
            found.append({"dir": str(path), "name": path.name, "ok": False,
                          "problems": ["manifest unreadable"]})
            continue
        ok, reasons = verify(path)
        found.append(
            {
                "dir": str(path),
                "name": manifest.get("name"),
                "kind": manifest.get("kind"),
                "version": manifest.get("version"),
                "date": manifest.get("date"),
                "source": manifest.get("source"),
                "files": len(manifest.get("files", {})),
                "ok": ok,
                "problems": reasons,
            }
        )
    return found


def find_edition(data_dir, name: str, kind: str | None = None) -> Path | None:
    """The newest shelved edition directory for a name, or None."""
    root = editions_dir(data_dir)
    if not root.is_dir():
        return None
    candidates = []
    for path in sorted(root.iterdir()):
        if not path.is_dir():
            continue
        try:
            manifest = load_manifest(path)
        except (ValueError, json.JSONDecodeError):
            continue
        if manifest.get("name") != name:
            continue
        if kind is not None and manifest.get("kind") != kind:
            continue
        candidates.append((manifest.get("version", ""), path))
    if not candidates:
        return None
    candidates.sort()
    return candidates[-1][1]


def verify_named(data_dir, name: str, kind: str | None = None) -> tuple[str, bool, list[str]]:
    """Verify the shelved edition for a name.

    Returns (edition_stamp, ok, reasons). The stamp is "name@version"
    when an edition is shelved, else "system" (nothing shelved: the
    caller falls back to whatever the system provides, and the stamp
    says so honestly). ok is False only when an edition exists and
    fails its seal - a tampered edition must never run.
    """
    path = find_edition(data_dir, name, kind)
    if path is None:
        return "system", True, []
    manifest = load_manifest(path)
    ok, reasons = verify(path)
    return f"{manifest.get('name')}@{manifest.get('version')}", ok, reasons
