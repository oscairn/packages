#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Builds an Oscairn repository (RFC 0031) from packages/, publishers/ and revoked.json.

Checks every release on the way: the records' schema, each archive's hash, archive safety and
every signature (through `oscairn-sign verify`), the publisher's key, the platform builds each
release claims, and that no build loads libraries from outside the system and its bundle. With --previous, also that no published release changed. Checks
revoked.json at least as strictly as a device reads it, since a list a device refuses takes the
whole repository off every device. Splits each release into pieces (RFC 0031, "Pieces: the
format"), and assembles from the pieces a ready-made archive per platform of each version, each
checked with `oscairn-sign verify`. Writes the repository's static tree to --out:

    repository.json            the root, unsigned: sign it with `oscairn-sign file`
    index/<sha256>.json.gz     the index
    index/<sha256>.delta.gz    with --previous-site, the change from the index published last
    revocations/<sha256>.json  the revocation list
    exports/<id>/<version>/<file>/index.html
                               each ready-made archive's stable address, a page sending the
                               browser on to <exportDownload><id>/<file>
    blobs.txt                  the SHA-256 of every piece and pieces list, each in --cache under that name
    exports.txt                "<package ID> <file>" for each ready-made archive, in --exports

Standard library only. Exits 1 and lists every problem when anything is wrong.
"""
import argparse
import datetime
import gzip
import hashlib
import html
import json
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

MAX_ARCHIVE = 1900 * 2**20  # GitHub release assets must be under 2 GiB; headroom below it
MAX_RECORD = 1 << 20
MIN_SAMPLE = 1 << 20  # a file under Assets/ this large is a piece of its own, kept as itself
PLATFORMS = {"linux-arm64", "linux-x86_64", "macos-arm64", "macos-x86_64", "windows-x86_64"}
ID = re.compile(r"^[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)+$")  # reverse-DNS
VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+_-]{0,63}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
HEX128 = re.compile(r"^[0-9a-f]{128}$")
DATE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
MAX_TEXT = 4096  # bytes; a device refuses longer text
# Each revocation list and the fields of its entries, beside `reason` and `date`, with their shapes.
REVOCATIONS = {"keys": {"key": HEX64}, "versions": {"id": ID, "version": VERSION}, "builds": {"sha256": HEX64}}


class Problems(list):
    def add(self, where, what):
        self.append(f"{where}: {what}")


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def key_id(key):
    return sha256(bytes.fromhex(key))[:16]


def load(path, problems):
    try:
        if path.stat().st_size > MAX_RECORD:
            raise ValueError("over 1 MiB")
        value = json.loads(path.read_bytes())
        if not isinstance(value, dict):
            raise ValueError("not a JSON object")
        return value
    except (OSError, ValueError) as e:
        problems.add(path, f"cannot be read: {e}")
        return None


def text(record, field, where, problems, pattern=None):
    value = record.get(field)
    if not isinstance(value, str) or not value or (pattern and not pattern.match(value)):
        problems.add(where, f"`{field}` is missing or malformed")
        return None
    return value


def check_format(record, name, where, problems):
    if record.get("format") != name or record.get("version") != 1:
        problems.add(where, f"not format {name}, version 1")
        return False
    return True


def read_publishers(root, problems):
    publishers = {}
    for path in sorted((root / "publishers").glob("*.json")):
        p = load(path, problems)
        if p is None or not check_format(p, "oscairn.repository-publisher", path, problems):
            continue
        pid = text(p, "id", path, problems, ID)
        text(p, "name", path, problems)
        key = text(p, "key", path, problems, HEX64)
        verification = text(p, "verification", path, problems)
        if verification and not verification.startswith("https://"):
            problems.add(path, "`verification` is not an https:// address")
        if pid and pid != path.stem:
            problems.add(path, f"its file is not named {pid}.json")
        if pid and key:
            publishers[pid] = p
    for pid in publishers:  # a publisher must not own another's namespace: com could claim com.example's packages
        for other in publishers:
            if other != pid and other.startswith(pid + "."):
                problems.add("publishers", f"publisher IDs do not nest: {pid} is a prefix of {other}")
    return publishers


def fetch(record, record_path, cache, problems):
    """The archive in --cache under its SHA-256, copied or downloaded and checked, or None."""
    archive = record.get("archive")
    if not isinstance(archive, dict):
        problems.add(record_path, "`archive` is missing")
        return None
    digest = text(archive, "sha256", record_path, problems, HEX64)
    size = archive.get("size")
    if not isinstance(size, int) or not 0 < size <= MAX_ARCHIVE:
        problems.add(record_path, f"`archive.size` is missing or over {MAX_ARCHIVE} bytes (GitHub release assets must be under 2 GiB)")
        return None
    if not digest:
        return None
    target = cache / digest
    if target.is_file() and sha256_file(target) == digest:
        return target
    partial = cache / (digest + ".partial")
    try:
        if "path" in archive:
            name = archive["path"]
            if not isinstance(name, str) or "/" in name or "\\" in name or name.startswith("."):
                problems.add(record_path, "`archive.path` must name a file beside the record")
                return None
            shutil.copyfile(record_path.parent / name, partial)
        elif isinstance(archive.get("url"), str) and archive["url"].startswith("https://"):
            with urllib.request.urlopen(archive["url"], timeout=60) as response, open(partial, "wb") as out:
                received = 0
                for chunk in iter(lambda: response.read(1 << 20), b""):
                    received += len(chunk)
                    if received > size:
                        raise ValueError("larger than `archive.size`")
                    out.write(chunk)
        else:
            problems.add(record_path, "`archive` needs a `path` beside the record or an https:// `url`")
            return None
        if partial.stat().st_size != size or sha256_file(partial) != digest:
            raise ValueError("not the size and SHA-256 the record gives")
        partial.replace(target)
        return target
    except (OSError, ValueError) as e:
        partial.unlink(missing_ok=True)
        problems.add(record_path, f"the archive cannot be fetched: {e}")
        return None


def verify(tool, archive, where, problems, platform=None):
    """`oscairn-sign verify`: archive safety, every hash and every signature, and each build's
    libraries from outside the system and its bundle; with a platform, that its builds are there.
    Its report, or None."""
    try:
        run = subprocess.run([tool, "verify", *(["--platform", platform] if platform else []), str(archive)], capture_output=True, text=True, timeout=900)
    except (OSError, subprocess.TimeoutExpired) as e:
        problems.add(where, f"oscairn-sign verify did not run: {e}")
        return None
    if run.returncode != 0:
        problems.add(where, f"the archive is refused: {run.stderr.strip()}")
        return None
    report = {"archive": None, "builds": [], "outside": [], "absent": []}
    for line in run.stdout.splitlines():
        words = line.split()
        if words[:1] == ["archive"] and len(words) == 3:
            report["archive"] = (words[1], words[2])
        elif words[:1] == ["build"] and len(words) == 6:
            report["builds"].append((words[1], words[2], words[3], words[4], words[5].split(",")))
        elif words[:1] == ["absent"] and len(words) == 5:
            report["absent"].append(words[1:])  # format, ID, version, platform
        elif words[:1] == ["outside"] and len(words) >= 6:
            report["outside"].append(line.split(None, 5)[1:])  # format, ID, version, platform, what
    return report


def facts(archive):
    """What the index says of an archive besides its pieces: its manifest, the manifest's hash and size, and its types."""
    with zipfile.ZipFile(archive) as z:  # only after oscairn-sign verify accepted it
        manifest_bytes = z.read("library.json")
    manifest = json.loads(manifest_bytes)
    types = {}
    for item in manifest["items"]:
        kind = "oscairn." + item["kind"]
        types[kind] = types.get(kind, 0) + 1
    for plugin in manifest["plugins"]:
        types[plugin["format"]] = types.get(plugin["format"], 0) + 1
    if manifest["kind"] == "asset":
        types["oscairn.asset"] = 1
    return manifest, {"sha256": sha256(manifest_bytes), "size": len(manifest_bytes)}, types


def store(cache, temporary):
    """A blob into --cache under its SHA-256, once: the same piece in two versions is one blob."""
    digest, size = sha256_file(temporary), temporary.stat().st_size
    if (cache / digest).is_file():
        temporary.unlink()
    else:
        temporary.replace(cache / digest)
    return digest, size


def zip_entry(name, size):
    """An entry as pieces and ready-made archives write it: the same bytes for the same content (RFC 0031)."""
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    info.create_system = 3
    info.file_size = size
    return info


def copy_into(out, name, size, source):
    with out.open(zip_entry(name, size), "w", force_zip64=size >= 1 << 31) as to:
        shutil.copyfileobj(source, to, 1 << 20)


def pieces(archive, manifest, cache):
    """The archive split into pieces (RFC 0031, "Pieces: the format"), each a blob in --cache: the
    pieces list's entries, in the order a device fetches them."""
    builds = {b["path"]: b["platform"] for p in manifest["plugins"] for b in p["builds"]}
    items = {i["path"] for i in manifest["items"]}
    groups = {}
    with zipfile.ZipFile(archive) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            name, parts = info.filename, info.filename.split("/")
            build = next((p for p in ("/".join(parts[:k]) for k in range(len(parts), 0, -1)) if p in builds), None)
            if name in ("library.json", "library.sig"):
                key = ("file", name, None)
            elif build:
                key = ("zip", build, builds[build])
            elif "/".join(parts[:2]) in items:
                key = ("zip", "/".join(parts[:2]), None)
            elif parts[0] == "Assets" and info.file_size >= MIN_SAMPLE:
                key = ("file", name, None)
            else:
                key = ("zip", parts[0], None)
            groups.setdefault(key, []).append(info)
        temporary = cache / ".piece.partial"
        out = []
        for (kind, path, platform), infos in sorted(groups.items(), key=lambda g: (g[0][2] or "", g[0][1] not in ("library.json", "library.sig"), g[0][1])):
            if kind == "file":
                with z.open(infos[0]) as source, open(temporary, "wb") as to:
                    shutil.copyfileobj(source, to, 1 << 20)
            else:
                with zipfile.ZipFile(temporary, "w") as to:
                    for info in sorted(infos, key=lambda i: i.filename):
                        with z.open(info) as source:
                            copy_into(to, info.filename, info.file_size, source)
            digest, size = store(cache, temporary)
            piece = {"sha256": digest, "size": size, "installed": sum(i.file_size for i in infos), "type": kind, "path": path}
            if platform:
                piece["platform"] = platform
            out.append(piece)
    return out


def assemble(path, listed, platform, cache):
    """A ready-made archive (RFC 0031) from the pieces alone: the manifest and its signature, what
    every platform shares, and this platform's builds (none for None)."""
    with zipfile.ZipFile(path, "w") as out:
        for piece in listed:
            if piece.get("platform") not in (None, platform):
                continue
            blob = cache / piece["sha256"]
            if piece["type"] == "file":
                with open(blob, "rb") as source:
                    copy_into(out, piece["path"], piece["installed"], source)
            else:
                with zipfile.ZipFile(blob) as z:
                    for info in z.infolist():
                        with z.open(info) as source:
                            copy_into(out, info.filename, info.file_size, source)


def version_key(version):
    return [(0, int(part), "") if part.isdigit() else (1, 0, part) for part in re.split(r"[.+-]", version)]


def read_release(path, root, publishers, tool, cache, problems):
    r = load(path, problems)
    if r is None or not check_format(r, "oscairn.repository-release", path, problems):
        return None
    before = len(problems)
    pid = text(r, "id", path, problems, ID)
    release = text(r, "release", path, problems, VERSION)
    publisher = text(r, "publisher", path, problems, ID)
    text(r, "summary", path, problems)
    text(r, "license", path, problems)
    if (pid and path.parent.name != pid) or (release and path.stem != release):
        problems.add(path, "not at packages/<id>/<release>.json")
    if r.get("channel", "stable") not in ("stable", "beta"):
        problems.add(path, "`channel` is not stable or beta")
    if not isinstance(r.get("tags", []), list) or not all(isinstance(t, str) for t in r.get("tags", [])):
        problems.add(path, "`tags` is not a list of words")
    if r.get("yanked") is not None and not isinstance(r["yanked"], str):
        problems.add(path, "`yanked` is not null or a reason")
    if not isinstance(r.get("requires", {}), dict):
        problems.add(path, "`requires` is not an object")
    claimed = r.get("platforms", [])
    if not isinstance(claimed, list) or not set(claimed) <= PLATFORMS:
        problems.add(path, f"`platforms` names a platform not in {sorted(PLATFORMS)}")
        claimed = []
    if publisher and publisher not in publishers:
        problems.add(path, f"no publishers/{publisher}.json")
    elif pid and publisher and not pid.startswith(publisher + "."):
        problems.add(path, f"the package ID is not under its publisher's ID {publisher}")
    if len(problems) > before:
        return None

    archive = fetch(r, path, cache, problems)
    report = archive and verify(tool, archive, path, problems)
    if not report:
        return None
    signer = key_id(publishers[publisher]["key"])
    if report["archive"] is None or report["archive"][0] != pid:
        problems.add(path, f"the archive's ID is not {pid}")
    elif report["archive"][1] != signer:
        problems.add(path, f"the archive is not signed by its publisher's key {signer} (it is {report['archive'][1]})")
    for fmt, plugin, version, platform, signers in report["builds"]:
        if signer not in signers:
            problems.add(path, f"the {platform} build of {fmt} {plugin} {version} is not signed by its publisher's key {signer}")
    for fmt, plugin, version, platform in report["absent"]:  # an archive made for one device is not a submission
        problems.add(path, f"the archive leaves out the {platform} build of {fmt} {plugin} {version} its manifest lists; submit the whole release")
    for fmt, plugin, version, platform, what in report["outside"]:
        problems.add(path, f"the {platform} build of {fmt} {plugin} {version} loads a library from outside the system and its bundle, which a device does not have: {what}")
    plugins = {(b[0], b[1], b[2]) for b in report["builds"]}
    for plugin in sorted(plugins):
        have = {b[3] for b in report["builds"] if b[:3] == plugin}
        for platform in claimed:
            if platform not in have:
                problems.add(path, f"claims {platform} but {plugin[0]} {plugin[1]} {plugin[2]} has no build for it")
    if claimed and not plugins:
        problems.add(path, "claims platforms but holds no plugin builds")
    if len(problems) > before:
        return None

    contents, manifest, types = facts(archive)
    # The record's license and source are the archive's own (its §6 directions travel inside it).
    if contents.get("license") and r.get("license") != contents["license"]:
        problems.add(path, f"`license` is not the archive's ({contents['license']})")
    source = r.get("source")
    if source is not None and not (isinstance(source, dict) and set(source) == {"url", "revision"} and all(isinstance(v, str) for v in source.values())):
        problems.add(path, "`source` is not {\"url\", \"revision\"}")
    elif source != contents.get("source"):
        problems.add(path, f"`source` is not the archive's ({json.dumps(contents.get('source'))})")
    # The asset libraries it needs, as the archive names them; each is checked against the repository in main().
    needs = contents.get("dependencies", {}).get("assets", [])
    if r.get("dependencies", {"assets": []}) != {"assets": needs} and not (needs == [] and "dependencies" not in r):
        problems.add(path, f"`dependencies` is not the archive's ({json.dumps({'assets': needs})})")
    if len(problems) > before:
        return None
    r["name"] = contents["name"]
    r["_kind"], r["_needs"], r["_archive"], r["_manifest"], r["_signer"] = contents["kind"], needs, archive, contents, signer
    return r, {
        "version": release,
        "channel": r.get("channel", "stable"),
        "manifest": manifest,
        "pieces": None,  # written in main() with the sizes below, once every release is checked
        "packageSignature": True,
        # The packages it needs, as RFC 0031's index names them: for now the asset libraries its archive names.
        "requires": {**r.get("requires", {}), **({"packages": needs} if needs else {})},
        "types": types,
        "platforms": {},
        "common": {"size": 0, "installed": 0},
        "estimates": {},
        "yanked": r.get("yanked"),
    }


def unchanged(previous, root, problems):
    """Published releases never change, but for `yanked`; a publisher's key never changes."""
    for old_path in sorted((previous / "packages").glob("*/*.json")):
        new_path = root / "packages" / old_path.relative_to(previous / "packages")
        old = load(old_path, problems)
        new = load(new_path, problems) if new_path.is_file() else None
        if new is None:
            problems.add(new_path, "a published release was removed; yank it instead")
        elif old is not None and {k: v for k, v in old.items() if k != "yanked"} != {k: v for k, v in new.items() if k != "yanked"}:
            problems.add(new_path, "a published release changed; publish a new version instead")
    for old_path in sorted((previous / "publishers").glob("*.json")):
        new_path = root / "publishers" / old_path.name
        old, new = load(old_path, problems), load(new_path, problems) if new_path.is_file() else None
        if old and (new is None or old.get("key") != new.get("key")):
            problems.add(new_path, "a publisher's key changed or was removed; key replacement is not built yet")


def read_revocations(path, config, problems):
    """revoked.json, or None when anything in it is wrong. A device (core/control/src/repository.cpp,
    revocations()) refuses the whole repository over a list it cannot read: the format and version
    1, all three lists, each entry an object with its fields as text of at most 4096 bytes, a
    non-empty reason, a YYYY-MM-DD date, keys and digests 64 lowercase hex digits. This is
    stricter: no unknown or repeated member (a device reads the first of a repeated one, Python
    the last), no repeated entry, ID and version shapes as in a release, real dates, a version
    that is the number 1 (Python takes true and 1.0 for 1, which a device refuses), and no
    revoking this repository's own root or index key, which makes devices refuse the root."""
    where, before = "revoked.json", len(problems)

    def members(pairs):
        names = [name for name, _ in pairs]
        repeated = sorted({name for name in names if names.count(name) > 1})
        if repeated:
            raise ValueError(f"a member is named twice: {', '.join(repeated)}")
        return dict(pairs)

    def constant(name):
        raise ValueError(f"{name} is not JSON")

    try:
        data = path.read_bytes()
        if len(data) > MAX_RECORD:
            raise ValueError("over 1 MiB")
        revoked = json.loads(data.decode("utf-8"), object_pairs_hook=members, parse_constant=constant)
        if not isinstance(revoked, dict):
            raise ValueError("not a JSON object")
    except (OSError, ValueError) as e:
        problems.add(where, f"cannot be read: {e}")
        return None
    if revoked.get("format") != "oscairn.repository-revocations" or type(revoked.get("version")) is not int or revoked["version"] != 1:
        problems.add(where, "not format oscairn.repository-revocations, version 1")
    for name in sorted(set(revoked) - {"format", "version", *REVOCATIONS}):
        problems.add(where, f"unknown member `{name}`")
    own = {config.get("rootKey"): "root key", config.get("indexKey", {}).get("key") if isinstance(config.get("indexKey"), dict) else None: "index key"}
    for list_name, fields in REVOCATIONS.items():
        entries = revoked.get(list_name)
        if not isinstance(entries, list):
            problems.add(where, f"`{list_name}` is missing or not a list (all three lists are present, empty when nothing is in them)")
            continue
        seen = {}
        for n, entry in enumerate(entries):
            at = f"{where}: {list_name}[{n}]"
            if not isinstance(entry, dict):
                problems.add(at, "not an object")
                continue
            for name in sorted(set(entry) - set(fields) - {"reason", "date"}):
                problems.add(at, f"unknown field `{name}`")
            ok = True
            for name, shape in {**fields, "reason": None, "date": DATE}.items():
                value = entry.get(name)
                try:
                    size = len(value.encode("utf-8")) if isinstance(value, str) else 0
                except UnicodeEncodeError:  # a lone surrogate, as \ud800
                    size = -1
                if name not in entry:
                    problems.add(at, f"`{name}` is missing")
                elif not isinstance(value, str) or size < 0:
                    problems.add(at, f"`{name}` is not text")
                elif value.strip() == "":
                    problems.add(at, f"`{name}` is empty")
                elif size > MAX_TEXT:
                    problems.add(at, f"`{name}` is over {MAX_TEXT} bytes")
                elif shape is HEX64 and not HEX64.match(value):
                    problems.add(at, f"`{name}` is not 64 lowercase hex digits: {value!r}")
                elif shape is DATE and not (DATE.match(value) and valid_date(value)):
                    problems.add(at, f"`date` is not a real date as YYYY-MM-DD: {value!r}")
                elif shape is not None and not shape.match(value):
                    problems.add(at, f"`{name}` is malformed: {value!r}")
                else:
                    continue
                ok = False
            if not ok:
                continue
            what = tuple(entry[name] for name in fields)
            if what in seen:
                problems.add(at, f"repeats {list_name}[{seen[what]}] ({' '.join(what)})")
            seen.setdefault(what, n)
            if list_name == "keys" and entry["key"] in own:
                problems.add(at, f"revokes this repository's own {own[entry['key']]}: every device would refuse the repository")
    return revoked if len(problems) == before else None


def valid_date(value):
    try:
        datetime.date.fromisoformat(value)
        return True
    except ValueError:
        return False


def check_delegation(config, tool, problems):
    """The root key's signature over the index key: oscairn-index-1\\0, the key's hex and a newline."""
    root_key, index = config.get("rootKey", ""), config.get("indexKey", {})
    if not (HEX64.match(root_key) and isinstance(index, dict) and HEX64.match(index.get("key", "")) and HEX128.match(index.get("signature", ""))):
        problems.add("repository-config.json", "`rootKey` and `indexKey` are not set (see README.md, Keys)")
        return
    with tempfile.TemporaryDirectory() as temp:
        key_file, sig_file = Path(temp) / "index-key.pub", Path(temp) / "index-key.sig"
        key_file.write_text(index["key"] + "\n")
        sig_file.write_text(json.dumps([{"key": root_key, "signature": index["signature"]}]))
        run = subprocess.run([tool, "verify-file", str(key_file), str(sig_file)], capture_output=True, text=True, timeout=60)
        if run.returncode != 0:
            problems.add("repository-config.json", f"the root key's signature over the index key does not verify: {run.stderr.strip()}")


def write_named(out, folder, data, suffix):
    digest = sha256(data)
    (out / folder).mkdir(parents=True, exist_ok=True)
    (out / folder / (digest + suffix)).write_bytes(data)
    return {"path": f"{folder}/{digest}{suffix}", "sha256": digest, "size": len(data)}


def delta(previous, index, config, tool, out):
    """The change from the last published index to this one, for devices that have it: each
    package record that changed, whole, and the IDs removed. Written only when the previous root
    verifies with this repository's index key and its index is the one it names; otherwise
    devices fetch the whole index, so none is listed."""
    if not previous or not (previous / "repository.json").is_file():
        return []
    try:
        run = subprocess.run([tool, "verify-file", str(previous / "repository.json"), str(previous / "repository.sig")], capture_output=True, text=True, timeout=60)
        root = json.loads((previous / "repository.json").read_bytes())
        if run.returncode != 0 or config.get("indexKey", {}).get("key") not in run.stdout.split():
            raise ValueError("the previous root is not signed by the index key")
        if not HEX64.match(root["index"]["sha256"]) or root["index"]["path"] != f"index/{root['index']['sha256']}.json.gz":
            raise ValueError("the previous root names its index wrongly")
        gz = (previous / root["index"]["path"]).read_bytes()
        if sha256(gz) != root["index"]["sha256"]:
            raise ValueError("the previous index is not the one its root names")
        before = json.loads(gzip.decompress(gz))
    except (OSError, ValueError, KeyError, TypeError) as e:
        print(f"no delta: {e}", file=sys.stderr)
        return []
    if before.get("repository") != index["repository"] or not isinstance(before.get("serial"), int) or before["serial"] >= index["serial"]:
        return []
    old = {p["id"]: p for p in before.get("packages", [])}
    new = {p["id"]: p for p in index["packages"]}
    change = {"format": "oscairn.repository-delta", "version": 1, "repository": index["repository"], "from": before["serial"], "serial": index["serial"],
              "packages": [p for i, p in sorted(new.items()) if old.get(i) != p], "removed": sorted(set(old) - set(new))}
    named = write_named(out, "index", gzip.compress(dumps(change), mtime=0), ".delta.gz")
    return [dict(named, **{"from": before["serial"]})]


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", type=Path, default=Path("."), help="the submissions repository (default: here)")
    ap.add_argument("--oscairn-sign", required=True, help="the oscairn-sign tool")
    ap.add_argument("--cache", type=Path, required=True, help="where archives are kept by SHA-256")
    ap.add_argument("--out", type=Path, required=True, help="where the repository's tree is written")
    ap.add_argument("--serial", type=int, required=True, help="the index's serial; higher than any before")
    ap.add_argument("--previous", type=Path, help="the repository before this change, to check nothing published changed")
    ap.add_argument("--previous-site", type=Path, help="the last published root, its signature and index, to write a delta from")
    ap.add_argument("--check", action="store_true", help="for a pull request: the root and index keys need not be set")
    ap.add_argument("--exports", type=Path, help="where the ready-made archives are written (default: made, checked and dropped)")
    args = ap.parse_args()
    root, problems = args.repo, Problems()
    args.cache.mkdir(parents=True, exist_ok=True)

    config = load(root / "repository-config.json", problems) or {}
    text(config, "repository", "repository-config.json", problems, ID)
    blob_base = text(config, "blobBase", "repository-config.json", problems)
    if blob_base and not blob_base.startswith("https://"):
        problems.add("repository-config.json", "`blobBase` is not an https:// address")
    export_download = config.get("exportDownload")  # none: no ready-made archives
    if export_download is not None and not (isinstance(export_download, str) and export_download.startswith("https://") and export_download.endswith("-")):
        problems.add("repository-config.json", "`exportDownload` is not an https:// address ending in `-` (the tag's start, as `blobBase`)")
    if not args.check or config.get("rootKey"):
        check_delegation(config, args.oscairn_sign, problems)
    if args.previous:
        unchanged(args.previous, root, problems)

    publishers = read_publishers(root, problems)
    packages = {}
    for path in sorted((root / "packages").glob("*/*.json")):
        got = read_release(path, root, publishers, args.oscairn_sign, args.cache, problems)
        if got:
            packages.setdefault(got[0]["id"], []).append(got)
    # A package keeps its publisher: a second publisher owning the prefix could otherwise publish
    # a newest release and take the package over on devices that trust by publisher key.
    for pid, releases in sorted(packages.items()):
        owners = {r["publisher"] for r, _ in releases}
        if len(owners) > 1:
            problems.add(f"packages/{pid}", f"a package keeps its publisher; these releases name {sorted(owners)}")
    # Dependencies: every asset library a release needs is an asset package here, at the version it needs or later.
    for pid, releases in sorted(packages.items()):
        for r, _ in releases:
            for need in r["_needs"]:
                have = [x for x, _ in packages.get(need.get("id"), []) if x["_kind"] == "asset" and x.get("yanked") is None]
                if not any(version_key(x["release"]) >= version_key(need.get("version", "")) for x in have):
                    problems.add(f"packages/{pid}/{r['release']}.json", f"needs the asset library {need.get('id')} {need.get('version')} or later, which this repository does not have")

    revoked = read_revocations(root / "revoked.json", config, problems)
    if revoked is not None:
        releases = [r for rs in packages.values() for r, _ in rs]
        # A revoked publisher key: devices list every version it signed as withdrawn (by the
        # index's publisher key ID) and never install one, and report installed copies. So its
        # published releases stay listed, which is how devices say why; a release not yet
        # published (not in --previous; every release without it) is refused.
        revoked_keys = {e["key"]: n for n, e in enumerate(revoked["keys"])}
        for pid, publisher in sorted(publishers.items()):
            n = revoked_keys.get(publisher["key"])
            if n is None:
                continue
            for r in releases:
                if r["publisher"] == pid and not (args.previous and (args.previous / "packages" / r["id"] / f"{r['release']}.json").is_file()):
                    problems.add(f"packages/{r['id']}/{r['release']}.json", f"its publisher's key is revoked (revoked.json: keys[{n}]); no new release signed by it is accepted")
            print(f"note: publishers/{pid}.json: its key is revoked (revoked.json: keys[{n}]); its published releases stay listed, and devices show them withdrawn and never install them", file=sys.stderr)
        published = {(r["id"], r["release"]) for r in releases}
        for n, e in enumerate(revoked["versions"]):
            if (e["id"], e["version"]) not in published:
                print(f"warning: revoked.json: versions[{n}]: {e['id']} {e['version']} is not a release in this repository", file=sys.stderr)

    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1

    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary:
        exports_dir = args.exports or Path(temporary)
        exports_dir.mkdir(parents=True, exist_ok=True)
        index_packages, blobs, exported = publish(packages, publishers, revoked, export_download, args, exports_dir, problems)
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1
    index = {"format": "oscairn.repository-index", "version": 1, "repository": config["repository"], "serial": args.serial, "packages": index_packages}
    # Each ready-made archive's stable address, a page that sends the browser on to the file
    # (GitHub Pages sends no redirects of its own): exports/<package ID>/<version>/<file name>/.
    for pid, version, name in exported:
        page = out / "exports" / pid / version / name / "index.html"
        page.parent.mkdir(parents=True, exist_ok=True)
        target = html.escape(f"{export_download}{pid}/{name}", quote=True)
        page.write_text(f'<!doctype html>\n<html lang="en"><meta charset="utf-8"><meta name="robots" content="noindex">\n'
                        f'<meta http-equiv="refresh" content="0; url={target}">\n<title>{html.escape(name)}</title>\n'
                        f'<p>Downloading <a href="{target}">{html.escape(name)}</a>, a ready-made Oscairn archive. If nothing happens, follow the link.</p>\n')
    if export_download:
        (out / "exports.txt").write_text("".join(f"{pid} {name}\n" for pid, _, name in exported))
    root_file = {
        "format": "oscairn.repository",
        "version": 1,
        "repository": config["repository"],
        "name": config.get("name", config["repository"]),
        "serial": args.serial,
        "rootKey": config.get("rootKey", ""),
        "indexKey": config.get("indexKey", {}),
        "index": write_named(out, "index", gzip.compress(dumps(index), mtime=0), ".json.gz"),
        "revocations": write_named(out, "revocations", dumps(revoked), ".json"),
        "deltas": delta(args.previous_site, index, config, args.oscairn_sign, out),
        "blobBase": blob_base,
        "mirrors": config.get("mirrors", []),
    }
    (out / "repository.json").write_bytes(dumps(root_file) + b"\n")
    (out / "blobs.txt").write_text("".join(b + "\n" for b in sorted(blobs)))
    print(f"serial {args.serial}: {len(index_packages)} packages, {sum(len(p['versions']) for p in index_packages)} versions, {len(blobs)} blobs, {len(exported)} ready-made archives")
    return 0


def publish(packages, publishers, revoked, export_download, args, exports_dir, problems):
    """Each release split into pieces (blobs in --cache, deduplicated across versions) and its
    ready-made archives assembled from them into exports_dir, each checked with `oscairn-sign
    verify` (RFC 0031). Returns the index's packages, every blob and the archives made."""
    revoked_builds = {e["sha256"] for e in revoked["builds"]}
    withdrawn = {(e["id"], e["version"]) for e in revoked["versions"]}
    revoked_keys = {e["key"] for e in revoked["keys"]}
    index_packages, blobs, exported = [], set(), []
    for pid in sorted(packages):
        releases = sorted(packages[pid], key=lambda g: version_key(g[0]["release"]))
        for r, v in releases:
            listed = pieces(r["_archive"], r["_manifest"], args.cache)
            (args.cache / ".pieces.partial").write_bytes(dumps({"format": "oscairn.repository-pieces", "version": 1, "pieces": listed}))
            digest, size = store(args.cache, args.cache / ".pieces.partial")
            v["pieces"] = {"sha256": digest, "size": size}
            blobs.add(digest)
            for p in listed:
                sizes = v["platforms"].setdefault(p["platform"], {"size": 0, "installed": 0}) if "platform" in p else v["common"]
                sizes["size"] += p["size"]
                sizes["installed"] += p["installed"]
                blobs.add(p["sha256"])
            # Its ready-made archives: one per platform with builds, or one for a package with none.
            targets = sorted(v["platforms"]) or [None]
            builds = {}
            for plugin in r["_manifest"]["plugins"]:
                for b in plugin["builds"]:
                    builds.setdefault(b["platform"], set()).add(b["sha256"])
            if (not export_download or (pid, r["release"]) in withdrawn or publishers[r["publisher"]]["key"] in revoked_keys
                    or any(v["common"]["size"] + (v["platforms"][t]["size"] if t else 0) > MAX_ARCHIVE for t in targets)):
                v["noExport"] = True
                continue
            missing = [t for t in targets if t and builds.get(t, set()) & revoked_builds]  # a revoked build's platform alone (decided 2026-11-12)
            for t in targets:
                if t in missing:
                    continue
                name = f"{pid}-{r['release']}-{t}.olib" if t else f"{pid}-{r['release']}.olib"
                assemble(exports_dir / name, listed, t, args.cache)
                where = f"packages/{pid}/{r['release']}.json: its ready-made archive {name}"
                report = verify(args.oscairn_sign, exports_dir / name, where, problems, t)
                have = sum(any(b["platform"] == t for b in p["builds"]) for p in r["_manifest"]["plugins"])  # each plugin's build for t
                if report and (report["archive"] != (pid, r["_signer"]) or any(b[3] != t for b in report["builds"]) or len(report["builds"]) != have):
                    problems.add(where, "is not the release's archive for that platform: " + json.dumps(report))
                exported.append((pid, r["release"], name))
            if missing:
                v["noExport"] = missing
        newest = releases[-1][0]
        publisher = publishers[newest["publisher"]]
        index_packages.append({
            "id": pid,
            "name": newest["name"],
            "summary": newest["summary"],
            "publisher": {"id": publisher["id"], "name": publisher["name"], "key": key_id(publisher["key"])},
            "license": newest["license"],
            "tags": newest.get("tags", []),
            "versions": [v for _, v in releases],
        })
    return index_packages, blobs, exported


if __name__ == "__main__":
    sys.exit(main())
