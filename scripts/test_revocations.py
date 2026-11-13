#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""build-index.py's checks of revoked.json, on a sample repository made here.

usage: test_revocations.py <oscairn-sign>

Makes throwaway root, index and publisher keys, a publisher and one signed release, then builds
the index with revocation lists: valid ones pass and are published as given; each malformed one
is refused with a message naming the entry and the problem (a list missing, a key that is not 64
hex digits, a bad date, an empty reason, an unknown field, a repeated entry or member, revoking
the root or index key, and more). A withdrawn version naming no release is a warning, and a
revoked publisher key keeps its published releases listed but refuses a new one. The keys live
only in a temporary folder.
"""
import gzip
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
BUILDER = HERE / "build-index.py"
HEX = "ab" * 32
failures = 0


def check(ok, what):
    global failures
    print(("ok   " if ok else "FAIL ") + what)
    failures += not ok


def run(*command):
    return subprocess.run([str(c) for c in command], capture_output=True, text=True, timeout=300)


def main(tool):
    temp = Path(tempfile.mkdtemp(prefix="oscairn-revocations-test-"))
    try:
        keys, made = temp / "keys", {}
        for name in ("publisher", "root", "index"):
            out = run(tool, "key", keys / f"{name}.key")
            assert out.returncode == 0, out.stderr
            _, _, made[name] = out.stdout.split()

        repo = temp / "repo"
        (repo / "publishers").mkdir(parents=True)
        (temp / "index-key.pub").write_text(made["index"] + "\n")
        out = run(tool, "file", keys / "root.key", temp / "index-key.pub", temp / "index-key.sig")
        assert out.returncode == 0, out.stderr
        signature = json.loads((temp / "index-key.sig").read_text())[0]["signature"]
        (repo / "repository-config.json").write_text(json.dumps({
            "repository": "com.example.repo", "name": "Example", "blobBase": "https://example.com/blobs-", "mirrors": [],
            "rootKey": made["root"], "indexKey": {"key": made["index"], "signature": signature}}))
        (repo / "publishers" / "com.example.json").write_text(json.dumps({
            "format": "oscairn.repository-publisher", "version": 1, "id": "com.example", "name": "Example Audio",
            "key": made["publisher"], "verification": "https://example.com/.well-known/oscairn-publisher.json"}))

        def release(version):
            folder = repo / "packages" / "com.example.organ"
            folder.mkdir(parents=True, exist_ok=True)
            build = temp / "Organ.clap"
            build.write_bytes(b"not really a plugin " + version.encode())
            Path(str(build) + ".sig").unlink(missing_ok=True)
            out = run(tool, "build", keys / "publisher.key", "clap", "com.example.organ", version, "linux-arm64", build)
            assert out.returncode == 0, out.stderr
            out = run(tool, "plugin-package", "--license", "MIT", "--key", keys / "publisher.key",
                      folder / f"{version}.olib", "clap", "com.example.organ", version, "Example Organ", f"linux-arm64={build}")
            assert out.returncode == 0, out.stderr
            data = (folder / f"{version}.olib").read_bytes()
            (folder / f"{version}.json").write_text(json.dumps({
                "format": "oscairn.repository-release", "version": 1, "id": "com.example.organ", "release": version,
                "publisher": "com.example", "summary": "A tonewheel organ", "license": "MIT", "platforms": ["linux-arm64"],
                "archive": {"path": f"{version}.olib", "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}, "yanked": None}))

        release("2.1.0")
        base = {"format": "oscairn.repository-revocations", "version": 1, "keys": [], "versions": [], "builds": []}
        entry = {"keys": {"key": HEX, "reason": "Its private key was published", "date": "2026-11-11"},
                 "versions": {"id": "com.example.organ", "version": "2.1.0", "reason": "Deletes user files", "date": "2026-11-11"},
                 "builds": {"sha256": "cd" * 32, "reason": "Malware found by a scanner", "date": "2026-11-12"}}

        def build(revoked, *extra, raw=None):
            (repo / "revoked.json").write_bytes(raw if raw is not None else json.dumps(revoked).encode())
            site = temp / "site"
            shutil.rmtree(site, ignore_errors=True)
            out = run(sys.executable, "-I", BUILDER, "--repo", repo, "--oscairn-sign", tool, "--cache", temp / "cache", "--out", site, "--serial", "3", *extra)
            return out, site

        def accepted(revoked, what, expect=None):
            out, site = build(revoked)
            published = None
            if out.returncode == 0:
                root = json.loads((site / "repository.json").read_text())
                published = json.loads((site / root["revocations"]["path"]).read_bytes())
            check(out.returncode == 0 and published == revoked and (expect is None or expect in out.stderr), what + ": " + out.stderr.strip())

        def refused(revoked, expect, what, raw=None):
            out, _ = build(revoked, raw=raw)
            check(out.returncode == 1 and expect in out.stderr, what + ": " + out.stderr.strip())

        # Valid lists.
        accepted(base, "three empty lists pass, published as given")
        accepted(dict(base, **{name: [e] for name, e in entry.items()}), "one entry in each list passes, published as given")
        accepted(dict(base, keys=[entry["keys"], dict(entry["keys"], key="ef" * 32)]), "two keys pass")

        # Malformed lists, each refused naming the entry and the problem.
        for name in ("keys", "versions", "builds"):
            refused({k: v for k, v in base.items() if k != name}, f"revoked.json: `{name}` is missing or not a list", f"a missing `{name}` list is refused")
        refused(dict(base, builds={}), "revoked.json: `builds` is missing or not a list", "a `builds` that is not a list is refused")
        refused(dict(base, format="oscairn.revocations"), "not format oscairn.repository-revocations, version 1", "another format is refused")
        refused(None, "not format oscairn.repository-revocations, version 1", "version true is refused (Python reads it as 1)",
                raw=json.dumps(base).replace('"version": 1', '"version": true').encode())
        refused(dict(base, version=1.0), "not format oscairn.repository-revocations, version 1", "version 1.0 is refused")
        refused(dict(base, notes="x"), "revoked.json: unknown member `notes`", "an unknown top-level member is refused")
        refused(dict(base, keys=["ab"]), "revoked.json: keys[0]: not an object", "an entry that is not an object is refused")
        refused(dict(base, keys=[dict(entry["keys"], key=HEX[:62])]), "revoked.json: keys[0]: `key` is not 64 lowercase hex digits", "a short key is refused")
        refused(dict(base, keys=[dict(entry["keys"], key=HEX.upper())]), "revoked.json: keys[0]: `key` is not 64 lowercase hex digits", "an uppercase key is refused")
        refused(dict(base, keys=[dict(entry["keys"], key=HEX[:16])]), "revoked.json: keys[0]: `key` is not 64 lowercase hex digits", "a 16-digit key ID is refused")
        refused(dict(base, builds=[dict(entry["builds"], sha256="xy" * 32)]), "revoked.json: builds[0]: `sha256` is not 64 lowercase hex digits", "a digest that is not hex is refused")
        refused(dict(base, versions=[dict(entry["versions"], date="2026-11-1")]), "revoked.json: versions[0]: `date` is not a real date as YYYY-MM-DD", "a short date is refused")
        refused(dict(base, versions=[dict(entry["versions"], date="11/11/2026")]), "revoked.json: versions[0]: `date` is not a real date as YYYY-MM-DD", "a date in another form is refused")
        refused(dict(base, versions=[dict(entry["versions"], date="2026-02-30")]), "revoked.json: versions[0]: `date` is not a real date as YYYY-MM-DD", "a date that is not a day is refused")
        refused(dict(base, builds=[{k: v for k, v in entry["builds"].items() if k != "date"}]), "revoked.json: builds[0]: `date` is missing", "an entry without a date is refused")
        refused(dict(base, keys=[dict(entry["keys"], reason="")]), "revoked.json: keys[0]: `reason` is empty", "an empty reason is refused")
        refused(dict(base, keys=[dict(entry["keys"], reason="  ")]), "revoked.json: keys[0]: `reason` is empty", "a blank reason is refused")
        refused(dict(base, keys=[dict(entry["keys"], reason="x" * 4097)]), "revoked.json: keys[0]: `reason` is over 4096 bytes", "a reason over 4096 bytes is refused")
        refused(dict(base, keys=[dict(entry["keys"], reason=7)]), "revoked.json: keys[0]: `reason` is not text", "a reason that is not text is refused")
        refused(None, "revoked.json: keys[0]: `reason` is not text", "a reason with a lone surrogate is refused",
                raw=json.dumps(dict(base, keys=[dict(entry["keys"], reason="\ud800")])).encode())
        refused(dict(base, versions=[dict(entry["versions"], id="")]), "revoked.json: versions[0]: `id` is empty", "a version without its package ID is refused")
        refused(dict(base, versions=[dict(entry["versions"], id="organ")]), "revoked.json: versions[0]: `id` is malformed: 'organ'", "a package ID that is not reverse-DNS is refused")
        refused(dict(base, versions=[dict(entry["versions"], version="2.1 beta")]), "revoked.json: versions[0]: `version` is malformed", "a malformed version is refused")
        refused(dict(base, builds=[dict(entry["builds"], comment="x")]), "revoked.json: builds[0]: unknown field `comment`", "an unknown field is refused")
        refused(dict(base, versions=[entry["versions"], dict(entry["versions"], reason="again")]),
                "revoked.json: versions[1]: repeats versions[0] (com.example.organ 2.1.0)", "a repeated version is refused")
        refused(dict(base, keys=[entry["keys"], entry["keys"]]), f"revoked.json: keys[1]: repeats keys[0] ({HEX})", "a repeated key is refused")
        refused(None, "revoked.json: cannot be read: a member is named twice: reason", "a repeated member is refused (a device reads the first, Python the last)",
                raw=json.dumps(dict(base, keys=[entry["keys"]])).replace('"reason":', '"reason": "first", "reason":').encode())
        refused(None, "revoked.json: cannot be read: NaN is not JSON", "NaN is refused", raw=json.dumps(dict(base, keys=[dict(entry["keys"], reason=float("nan"))])).encode())
        refused(None, "revoked.json: cannot be read", "a list that is not UTF-8 is refused", raw=json.dumps(base).encode("utf-16"))
        refused(None, "revoked.json: cannot be read", "a list that is not JSON is refused", raw=b'{"format": ')
        refused(dict(base, keys=[dict(entry["keys"], key=made["root"])]), "revoked.json: keys[0]: revokes this repository's own root key", "revoking the root key is refused")
        refused(dict(base, keys=[dict(entry["keys"], key=made["index"])]), "revoked.json: keys[0]: revokes this repository's own index key", "revoking the index key is refused")
        out, _ = build(dict(base, keys=[{"key": "AB"}], versions=[{"id": "com.example.organ"}]))
        check(out.returncode == 1 and all(m in out.stderr for m in ("keys[0]: `key` is not 64", "keys[0]: `reason` is missing", "versions[0]: `version` is missing", "versions[0]: `date` is missing")),
              "every problem is listed at once: " + out.stderr.strip())

        # A withdrawn version naming no release here is a warning, not a refusal.
        accepted(dict(base, versions=[dict(entry["versions"], version="9.9.9")]), "a withdrawn version naming no release is a warning",
                 "warning: revoked.json: versions[0]: com.example.organ 9.9.9 is not a release in this repository")
        out, _ = build(dict(base, versions=[entry["versions"]]))
        check(out.returncode == 0 and "warning" not in out.stderr, "and no warning for one naming a release: " + out.stderr.strip())

        # A revoked publisher key: its published release stays listed; a new release is refused.
        revoked_publisher = dict(base, keys=[dict(entry["keys"], key=made["publisher"])])
        previous = temp / "previous"
        shutil.copytree(repo, previous)
        release("2.2.0")
        out, site = build(revoked_publisher, "--previous", previous)
        check(out.returncode == 1 and "packages/com.example.organ/2.2.0.json: its publisher's key is revoked (revoked.json: keys[0])" in out.stderr
              and "2.1.0.json: its publisher" not in out.stderr, "a new release by a revoked key is refused, the published one is not: " + out.stderr.strip())
        for path in (repo / "packages" / "com.example.organ").glob("2.2.0.*"):
            path.unlink()
        out, site = build(revoked_publisher, "--previous", previous)
        check(out.returncode == 0 and "note: publishers/com.example.json: its key is revoked" in out.stderr, "a revoked key's published release still builds, with a note: " + out.stderr.strip())
        if out.returncode == 0:
            root = json.loads((site / "repository.json").read_text())
            index = json.loads(gzip.decompress((site / root["index"]["path"]).read_bytes()))
            check([v["version"] for p in index["packages"] for v in p["versions"]] == ["2.1.0"], "and stays listed, for devices to show withdrawn")
        out, _ = build(revoked_publisher)
        check(out.returncode == 1 and "2.1.0.json: its publisher's key is revoked" in out.stderr, "without --previous every release is new, so refused: " + out.stderr.strip())
    finally:
        shutil.rmtree(temp, ignore_errors=True)
    print("FAILED" if failures else "PASSED")
    return 1 if failures else 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    sys.exit(main(Path(sys.argv[1]).resolve()))
