#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Makes the files of a library-only release (README.md, "A library release") from a tree.

    make-library-release.py --tree <tree> --id <package id> --version X.Y.Z --name <name> --summary <text>
        --license <spdx> --license-file <file>... --source-url https://github.com/oscairn/<repo>
        --tag <tag> --revision <40-hex commit> [--tag-name <tag>]... [--package-dependency <id> <version>]...
        --out <folder>

<tree> holds Types/<package id>/<name>.json, the item types the package declares (nothing else is
allowed). <folder> gets oscairn-release.json, licenses.tar.gz and a copy of each type file: attach
every file of it to the release the tag names. Standard library only.
"""
import argparse
import hashlib
import json
import shutil
import sys
import tarfile
from pathlib import Path


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--tree", required=True, type=Path)
    ap.add_argument("--id", required=True)
    ap.add_argument("--version", required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--summary", required=True)
    ap.add_argument("--license", required=True)
    ap.add_argument("--license-file", action="append", required=True, type=Path)
    ap.add_argument("--source-url", required=True)
    ap.add_argument("--tag", required=True)
    ap.add_argument("--revision", required=True)
    ap.add_argument("--tag-name", action="append", default=[], help="a tag of the package (the record's `tags`), e.g. types")
    ap.add_argument("--package-dependency", nargs=2, action="append", default=[], metavar=("ID", "VERSION"))
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args()
    folder = args.tree / "Types" / args.id
    types = sorted(folder.glob("*.json")) if folder.is_dir() else []
    if not types:
        sys.exit(f"make-library-release: no {folder}/*.json")
    others = [p for p in args.tree.rglob("*") if p.is_file() and p not in types]
    if others:
        sys.exit(f"make-library-release: {others[0]} is not a type manifest under Types/{args.id}/")
    args.out.mkdir(parents=True, exist_ok=False)
    files = []
    for p in types:
        data = p.read_bytes()
        shutil.copyfile(p, args.out / p.name)
        files.append({"file": p.name, "path": f"Types/{args.id}/{p.name}", "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)})
    licenses = []
    with tarfile.open(args.out / "licenses.tar.gz", "w:gz") as tar:
        for p in args.license_file:
            data = p.read_bytes()
            tar.add(p, arcname=f"licenses/{p.name}", filter=lambda i: (setattr(i, "uid", 0), setattr(i, "gid", 0), i)[2])
            licenses.append({"file": f"licenses/{p.name}", "sha256": hashlib.sha256(data).hexdigest()})
    manifest = {
        "format": "oscairn.library-release", "version": 1, "kind": "library",
        "package": {"id": args.id, "version": args.version, "name": args.name, "summary": args.summary},
        "tags": args.tag_name, "files": files,
        "source": {"url": args.source_url, "revision": args.revision, "tag": args.tag},
        "license": args.license, "licenseFiles": licenses,
    }
    if args.package_dependency:
        manifest["dependencies"] = {"packages": [{"id": i, "version": v} for i, v in args.package_dependency]}
    (args.out / "oscairn-release.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"wrote {args.out}: oscairn-release.json, licenses.tar.gz, " + ", ".join(f["file"] for f in files))


if __name__ == "__main__":
    main()
