#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Imports a plugin release from one of Oscairn's forks (README.md, "Importing a release").

A fork's tag vX.Y.Z or oscairn-vX.Y.Z publishes a GitHub release holding oscairn-release.json
(the manifest, schemas/plugin-release.schema.json), one build file per platform and
licenses.tar.gz. Steps, each a command, as .github/workflows/import.yml runs them:

    import-release.py verify (--release <url> | --repo oscairn/<fork> --tag [oscairn-]vX.Y.Z | --from <url>)
                             --out <candidate> [--packages .] [--accept-license-change] [--first-release]
        Fetches the manifest and its files into <candidate>, and checks: the manifest against the
        schema; every file's SHA-256 and size; the source is an allowed Oscairn plugin repository and the tag is
        source.revision there; each build's libraries (Mach-O and ELF read here, so on any
        machine); the license and license files are the package's last release's. Writes
        plan.json, matrix.json (a runner for each platform) and sign.sh (the commands that sign
        it, for signing by hand). A build for a platform PLATFORMS has no row for (no Oscairn host
        to play it on) is left out of the candidate and said so, in the output, notice.txt and
        plan.json's notImported; a release with no other build is refused. The packages the
        manifest's dependencies.packages names must be here (not yanked, at that version or later),
        none the package itself, and none that needs the package back, directly or through others.
        An `init` (an init library archive and the Sound in it) is fetched and verified like a build;
        it stays in the candidate for plugin-check and is never put in the plugin's package. It is
        published beside the plugin, under README.md's init library exception: its package id must be
        <plugin id>.init, it is read without unpacking (INIT_FOLDERS only, no builds, no links, at
        most INIT_MAX_BYTES) and plan.json's initPackage says how it is signed and recorded.
        A package id with no release here is refused unless --first-release says this is its first
        release; the flag on an id that has a release is refused, and an id removed.json lists
        is refused either way. A first release is Oscairn's own publisher's (OWN_PUBLISHER), has
        no earlier license to compare with, and every other check is as strict; the output,
        first-release.txt and plan.json (firstRelease) show its license and source.
    import-release.py plugin-check --candidate <candidate> --platform <p> --plugin-check <tool>
        Plays each plugin of that platform's build through oscairn-plugin-check, on that platform
        (with --library <init library> --sound <Sound> when the manifest has an `init`).
    import-release.py sign --candidate <candidate> --key <key> --oscairn-sign <tool>
        What sign.sh does: signs each build and packs and signs the archive, and signs a copy of the init
        library with `oscairn-sign archive`. Nothing else sees the key.
    import-release.py record --candidate <candidate> --oscairn-sign <tool> [--packages .]
        Checks the archive with `oscairn-sign verify` against the publisher's key and writes
        packages/<id>/<version>.json and .olib, and the init library's the same way, after checking that
        what was signed holds the entries and bytes of the file plugin-check played.

Standard library only. Exits 1 and says every problem when anything is wrong.
"""
import argparse
import hashlib
import json
import re
import shlex
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCHEMA = HERE.parent / "schemas" / "plugin-release.schema.json"
MANIFEST = "oscairn-release.json"
LICENSES = "licenses.tar.gz"
FORKS = {f"https://github.com/oscairn/{name}" for name in ("dexed", "surge", "sfizz", "sst-effects", "setBfree", "arpeggiator")}
# First-party plugins, not forks.
FIRST_PARTY = {f"https://github.com/oscairn/{name}" for name in ("va", "sampler", "fm")}
# One row per platform: the runner plugin-check runs on, and the binary a build must be
# (Mach-O CPU type or ELF machine). Another platform is another row.
PLATFORMS = {
    "macos-arm64": {"runner": "macos-15", "macho": 0x0100000C},
    "linux-arm64": {"runner": "ubuntu-24.04-arm", "elf": 183},
    "linux-x86_64": {"runner": "ubuntu-24.04", "elf": 62},
}
# Oscairn's Linux allowlist (oscairn docs/plugin-porting.md, "The Linux allowlist"): what every
# image carries, and the newest glibc and libstdc++ symbol versions every image has.
LINUX_LIBRARIES = {
    "libc.so.6", "libm.so.6", "libmvec.so.1", "libdl.so.2", "libpthread.so.0", "librt.so.1", "ld-linux-aarch64.so.1",
    "ld-linux-x86-64.so.2",
    "libgcc_s.so.1", "libstdc++.so.6", "libasound.so.2", "libX11.so.6", "libXext.so.6", "libXrandr.so.2", "libXinerama.so.1",
    "libXcursor.so.1", "libXrender.so.1", "libfreetype.so.6", "libfontconfig.so.1",
}
NEWEST = {"GLIBC_": (2, 44), "GLIBCXX_": (3, 4, 34)}
MAX_FILE = 1900 * 2**20  # as the schema's build size
MAX_MANIFEST = 1 << 20
MAX_LICENSE = 16 << 20
# The init library exception (README.md): what a library published with its plugin may hold.
INIT_MAX_BYTES = 8 << 20  # the archive, and its entries' sizes added up
INIT_MAX_ENTRIES = 256
INIT_FOLDERS = ("Sounds", "Samples", "Waveforms", "Types", "Licenses")  # no Plugins/, no Assets/
MAX_SOUND = 4 << 20
MAX_BESIDE = 95 * 2**20  # an archive beside its record: GitHub refuses files over 100 MiB
PLUGIN_CHECK_SECONDS = 600
OWN_PUBLISHER = "org.oscairn"  # the only publisher a first release may be for


class Refused(Exception):
    pass


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def key_id(key):
    return hashlib.sha256(bytes.fromhex(key)).hexdigest()[:16]


def version_key(version):  # as build-index.py orders versions
    return [(0, int(part), "") if part.isdigit() else (1, 0, part) for part in re.split(r"[.+-]", version)]


def download(url, path, limit):
    """Streams url to path, refusing more than limit bytes."""
    received = 0
    with urllib.request.urlopen(url, timeout=120) as response, open(path, "wb") as out:
        for chunk in iter(lambda: response.read(1 << 20), b""):
            received += len(chunk)
            if received > limit:
                raise Refused(f"{url} is over {limit} bytes")
            out.write(chunk)


# The schema ----------------------------------------------------------------------------------

KEYWORDS = {"$schema", "$id", "title", "description", "type", "const", "enum", "pattern", "minLength", "maxLength",
            "minimum", "maximum", "properties", "required", "additionalProperties", "items", "minItems", "maxItems"}
TYPES = {"object": dict, "array": list, "string": str, "integer": int}


def validate(value, schema, where="manifest"):
    """The problems with value under schema: the JSON Schema keywords in KEYWORDS, and no others."""
    unknown = set(schema) - KEYWORDS
    if unknown:
        raise ValueError(f"the schema uses {sorted(unknown)}, which import-release.py does not check")
    kind = schema.get("type")
    if kind and (not isinstance(value, TYPES[kind]) or isinstance(value, bool)):
        return [f"{where} is not of type {kind}"]
    problems = []
    if "const" in schema and (value != schema["const"] or isinstance(value, bool)):
        problems.append(f"{where} is not {json.dumps(schema['const'])}")
    if "enum" in schema and value not in schema["enum"]:
        problems.append(f"{where} is not one of {schema['enum']}")
    if isinstance(value, str):
        if "pattern" in schema:
            if not (schema["pattern"].startswith("^") and schema["pattern"].endswith("$")):
                raise ValueError(f"the schema's pattern {schema['pattern']} is not anchored")
            if not re.fullmatch(schema["pattern"], value):
                problems.append(f"{where} does not match {schema['pattern']}")
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", len(value)):
            problems.append(f"{where} has a length out of range")
    if isinstance(value, int) and not isinstance(value, bool):
        if value < schema.get("minimum", value) or value > schema.get("maximum", value):
            problems.append(f"{where} is out of range")
    if isinstance(value, dict):
        for name in schema.get("required", []):
            if name not in value:
                problems.append(f"{where} has no `{name}`")
        properties = schema.get("properties", {})
        for name, item in value.items():
            if name in properties:
                problems += validate(item, properties[name], f"{where}.{name}")
            elif schema.get("additionalProperties", True) is False:
                problems.append(f"{where} has `{name}`, which the schema does not allow")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", len(value)):
            problems.append(f"{where} has a count of items out of range")
        if "items" in schema:
            for i, item in enumerate(value):
                problems += validate(item, schema["items"], f"{where}[{i}]")
    return problems


# Libraries -----------------------------------------------------------------------------------

def cstring(data, at, end):
    stop = data.find(b"\0", at, end)
    if at >= end or stop < 0:
        raise ValueError("a name without its end")
    return data[at:stop].decode("utf-8", "replace")


def macho_problems(data, cpu):
    """A macOS build may load only /usr/lib/ and /System/ libraries, with no LC_RPATH."""
    magic = struct.unpack_from(">I", data)[0]
    if magic in (0xCAFEBABE, 0xCAFEBABF):
        wide, count = magic == 0xCAFEBABF, struct.unpack_from(">I", data, 4)[0]
        if count > 16:
            raise ValueError("too many slices")
        problems, cpus = [], []
        for i in range(count):
            at = 8 + i * (32 if wide else 20)
            slice_cpu, = struct.unpack_from(">I", data, at)
            offset, size = struct.unpack_from(">QQ" if wide else ">II", data, at + 8)
            if offset + size > len(data):
                raise ValueError("a slice outside the file")
            cpus.append(slice_cpu)
            problems += thin_problems(data[offset:offset + size], None)
        return problems + ([] if cpu in cpus else [f"has no slice for CPU type {cpu:#x}"])
    return thin_problems(data, cpu)


def thin_problems(data, cpu):
    magic = struct.unpack_from("<I", data)[0]
    if magic not in (0xFEEDFACE, 0xFEEDFACF):
        raise ValueError("not a little-endian Mach-O file")
    file_cpu, ncmds, sizeofcmds = struct.unpack_from("<I", data, 4)[0], *struct.unpack_from("<II", data, 16)
    first = 32 if magic == 0xFEEDFACF else 28
    end = first + sizeofcmds
    if ncmds > 65536 or end > len(data):
        raise ValueError("load commands outside the file")
    problems = [] if cpu is None or file_cpu == cpu else [f"is CPU type {file_cpu:#x}, not {cpu:#x}"]
    at = first
    for _ in range(ncmds):
        cmd, size = struct.unpack_from("<II", data, at)
        if size < 8 or at + size > end:
            raise ValueError("a load command outside its table")
        if cmd in (0xC, 0x80000018, 0x8000001F, 0x20, 0x80000023):  # load, weak, reexport, lazy, upward
            name = cstring(data, at + struct.unpack_from("<I", data, at + 8)[0], at + size)
            if not name.startswith(("/usr/lib/", "/System/")):
                problems.append(f"loads {name}, which is not under /usr/lib/ or /System/")
        elif cmd == 0x8000001C:
            problems.append(f"has an LC_RPATH, {cstring(data, at + struct.unpack_from('<I', data, at + 8)[0], at + size)}")
        at += size
    return problems


def elf_problems(data, machine):
    """A Linux build may need only the allowlist's libraries and symbol versions, with no RPATH or RUNPATH."""
    if data[:4] != b"\x7fELF" or data[4] != 2 or data[5] != 1:
        raise ValueError("not a 64-bit little-endian ELF file")
    problems = []
    if struct.unpack_from("<H", data, 18)[0] != machine:
        problems.append(f"is ELF machine {struct.unpack_from('<H', data, 18)[0]}, not {machine}")
    phoff, = struct.unpack_from("<Q", data, 32)
    phsize, phnum = struct.unpack_from("<HH", data, 54)
    segments = [struct.unpack_from("<IIQQQQ", data, phoff + i * phsize) for i in range(phnum)]  # type, flags, offset, vaddr, paddr, filesz
    dynamic = next((s for s in segments if s[0] == 2), None)
    if dynamic is None:
        return problems  # static: it loads nothing

    def placed(address):
        for kind, _, offset, vaddr, _, size in segments:
            if kind == 1 and vaddr <= address < vaddr + size:
                return offset + address - vaddr
        raise ValueError("an address outside the file")

    entries = {}
    for at in range(dynamic[2], min(dynamic[2] + dynamic[5], len(data)) - 15, 16):
        tag, value = struct.unpack_from("<QQ", data, at)
        if tag == 0:
            break
        entries.setdefault(tag, []).append(value)
    if 5 not in entries:
        raise ValueError("no string table")
    strings = placed(entries[5][0])
    name = lambda offset: cstring(data, strings + offset, len(data))
    for offset in entries.get(1, []):
        if name(offset) not in LINUX_LIBRARIES:
            problems.append(f"needs {name(offset)}, which is not in every Oscairn image")
    for tag, what in ((15, "RPATH"), (29, "RUNPATH")):
        for offset in entries.get(tag, []):
            problems.append(f"has a {what}, {name(offset)}")
    if 0x6FFFFFFE in entries:  # VERNEED: each Verneed and its Vernaux list
        entry = placed(entries[0x6FFFFFFE][0])
        for _ in range(min(entries.get(0x6FFFFFFF, [0])[0], 4096)):
            count, _, aux, following = struct.unpack_from("<HHIII", data, entry)[1:]
            aux += entry
            for _ in range(count):
                version = name(struct.unpack_from("<I", data, aux + 8)[0])
                for prefix, newest in NEWEST.items():
                    if version.startswith(prefix):
                        parts = version[len(prefix):].split(".")
                        if not all(p.isdigit() and len(p) <= 6 for p in parts):
                            problems.append(f"needs {version}, not a released version")
                        elif tuple(map(int, parts)) > newest:
                            problems.append(f"needs {version}, newer than the image's {prefix}{'.'.join(map(str, newest))}")
                step = struct.unpack_from("<I", data, aux + 12)[0]
                if not step:
                    break
                aux += step
            if not following:
                break
            entry += following
    return list(dict.fromkeys(problems))  # a version libc.so.6 and libm.so.6 both give is said once


def library_problems(path, platform):
    row = PLATFORMS[platform]
    data = path.read_bytes()  # shortcut: whole build in memory, fine below MAX_FILE on a runner; stream it if builds grow
    try:
        return macho_problems(data, row["macho"]) if "macho" in row else elf_problems(data, row["elf"])
    except (ValueError, struct.error) as e:
        return [f"cannot be read: {e}"]


def removed_versions(packages):
    """removed.json's entries (README.md): versions maintainers deleted, which stay refused."""
    path = packages / "removed.json"
    return json.loads(path.read_bytes()).get("versions", []) if path.exists() else []


# The previous release ------------------------------------------------------------------------

def previous_release(packages, package_id):
    records = sorted((packages / "packages" / package_id).glob("*.json"), key=lambda p: version_key(p.stem))
    if not records:
        raise Refused(f"no release of {package_id} here yet: a first release of a new package id is imported only when asked for"
                      " (--first-release; the workflow's first-release input), or is submitted by hand (README.md)")
    path = records[-1]
    return path, json.loads(path.read_bytes())


def previous_license_files(path, record, temp):
    """Each license file in the previous release's archive, by name: its SHA-256."""
    archive = record["archive"]
    if "path" in archive:
        if "/" in archive["path"] or archive["path"].startswith("."):
            raise Refused(f"{path}: `archive.path` must name a file beside it")
        olib = path.parent / archive["path"]
    else:
        olib = temp / "previous.olib"
        download(archive["url"], olib, archive["size"])
    if sha256_file(olib) != archive["sha256"]:
        raise Refused(f"{path}: its archive is not the SHA-256 it gives")
    with zipfile.ZipFile(olib) as z:
        if z.getinfo("library.json").file_size > MAX_MANIFEST:
            raise Refused(f"{path}: its archive's manifest is over 1 MiB")
        files = json.loads(z.read("library.json")).get("files", {})
    return {name[len("Licenses/"):]: digest for name, digest in files.items() if name.startswith("Licenses/")}


# Package dependencies -----------------------------------------------------------------------

def live_releases(packages, package_id):
    """The package's records here that are not yanked, newest first: (version, record)."""
    records = []
    for path in (packages / "packages" / package_id).glob("*.json"):
        r = json.loads(path.read_bytes())
        if r.get("yanked") is None:
            records.append((r["release"], r))
    return sorted(records, key=lambda x: version_key(x[0]), reverse=True)


def package_dependency_problems(packages, package_id, needs, arriving=()):
    """As build-index.py checks `dependencies.packages` (each package here, of any kind, not yanked, at
    that version or later), and that none is the package itself or needs it back: the device
    refuses a cycle when it installs, and a release that makes one should not be published."""
    problems = []
    for need in needs:
        if need["id"] == package_id:
            problems.append(f"{package_id} cannot depend on itself (dependencies.packages names it)")
            continue
        if need["id"] in arriving:  # the init library, published in this very run (init_problems checks it)
            continue
        have = live_releases(packages, need["id"])
        if not any(version_key(v) >= version_key(need["version"]) for v, _ in have):
            problems.append(f"needs the package {need['id']} {need['version']} or later, which this repository does not have")
            continue
        # The newest release of it, and of what it needs in turn: does the chain come back to this package?
        seen, stack = set(), [(need["id"], [package_id, need["id"]])]
        while stack:
            current, chain = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            releases = live_releases(packages, current)
            for further in (releases[0][1].get("dependencies", {}).get("packages", []) if releases else []):
                if further.get("id") == package_id:
                    problems.append(f"a cycle of package dependencies: {' -> '.join(chain + [package_id])}")
                else:
                    stack.append((further.get("id"), chain + [further.get("id")]))
    return problems


# The init library (README.md, "The init library exception") ----------------------------------

EXECUTABLE_MAGIC = (b"\x7fELF", b"MZ", b"#!", b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf", b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe",
                    b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf", b"\xbe\xba\xfe\xca")


def init_entry_name_problem(name):
    """Why an archive entry's name is refused (as oscairn's reader refuses it), or None."""
    path = name[:-1] if name.endswith("/") else name
    if not path or len(name.encode("utf-8", "replace")) > 1024:
        return "has an empty or over-long name"
    if name.startswith("/") or "\\" in name or ":" in name or any(ord(c) < 32 or ord(c) == 127 for c in name):
        return "is absolute or names a drive, uses a backslash or has a control character"
    parts = path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return "climbs out or has an empty component"
    if any(part.startswith(".") for part in parts):
        return "is hidden"
    return None


def init_archive_problems(path, library_id, plugins, sound, license_, source):
    """What the init library exception refuses in the archive at `path`, read in place (nothing is
    unpacked here): every entry a safe path in INIT_FOLDERS, no link or executable, no plugin build,
    at most INIT_MAX_ENTRIES entries and INIT_MAX_BYTES, its manifest this library's (one id, one
    Sound, the license and source stated), every entry listed in `files` with its SHA-256, and the
    Sound's nodes the release's own plugins at the versions the release has. Returns (problems, manifest)."""
    problems = []
    try:
        z = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as e:
        return [f"is not a library archive: {e}"], None
    with z:
        infos = z.infolist()
        if len(infos) > INIT_MAX_ENTRIES:
            return [f"has {len(infos)} entries, over the {INIT_MAX_ENTRIES} an init library may have"], None
        names = [i.filename for i in infos]
        for name in sorted({n for n in names if names.count(n) > 1}):
            problems.append(f"has the entry {name} twice (a second library.json would be a second library id)")
        lowered = {}
        for n in names:
            lowered.setdefault(n.lower(), set()).add(n)
        problems += [f"has entries that differ only in case: {sorted(group)}" for group in lowered.values() if len(group) > 1]
        total, files = 0, {}
        for info in infos:
            name = info.filename
            why = init_entry_name_problem(name)
            if why:
                problems.append(f"entry {name!r} {why}")
                continue
            mode = info.external_attr >> 16 if info.create_system == 3 else 0
            if mode & 0o170000 == 0o120000:
                problems.append(f"entry {name} is a symbolic link")
                continue
            if mode & 0o170000 not in (0, 0o100000, 0o040000) or (mode & 0o111 and not name.endswith("/")):
                problems.append(f"entry {name} is not a plain file (mode {mode:o}): nothing executable goes in an init library")
                continue
            if info.flag_bits & 1:
                problems.append(f"entry {name} is encrypted")
                continue
            if name.endswith("/"):
                continue
            total += info.file_size
            if name in ("library.json", "library.sig"):
                if name == "library.json" and info.file_size > MAX_MANIFEST:
                    problems.append("library.json is over 1 MiB")
                continue
            if "/" not in name or name.split("/")[0] not in INIT_FOLDERS:
                problems.append(f"entry {name} is outside {', '.join(f + '/' for f in INIT_FOLDERS)} (an init library holds a Sound, its samples and waveforms, type manifests and license texts: no Plugins/, no Assets/)")
                continue
            files[name] = info
        if total > INIT_MAX_BYTES:
            problems.append(f"holds {total} bytes uncompressed, over the {INIT_MAX_BYTES} an init library may hold")
        if "library.json" not in names:
            problems.append("has no library.json")
        if problems:
            return problems, None
        # Entry by entry, each read no further than it declares; the bytes read are what is hashed and checked.
        data = {}
        try:
            for name, info in files.items():
                with z.open(info) as f:
                    body = f.read(info.file_size + 1)
                if len(body) != info.file_size:
                    problems.append(f"entry {name} is not the size it declares")
                    continue
                if body.startswith(EXECUTABLE_MAGIC):
                    problems.append(f"entry {name} is an executable file: nothing executable goes in an init library")
                data[name] = body
            manifest_bytes = z.read("library.json")
        except (zipfile.BadZipFile, NotImplementedError, RuntimeError, OSError, EOFError) as e:
            return problems + [f"cannot be read: {e}"], None
    if problems:
        return problems, None
    try:
        manifest = json.loads(manifest_bytes)
        if not isinstance(manifest, dict):
            raise ValueError
    except ValueError:
        return ["library.json is not a JSON object"], None
    for field, want in (("format", "oscairn.library"), ("version", 3), ("kind", "library"), ("id", library_id)):
        if manifest.get(field) != want or isinstance(manifest.get(field), bool):
            problems.append(f"library.json's {field} is {json.dumps(manifest.get(field))}, not {json.dumps(want)}")
    if manifest.get("plugins") != [] or manifest.get("assets") is not None:
        problems.append("library.json lists plugins or an asset library: an init library holds a Sound and its content, no plugin builds")
    if manifest.get("license") != license_:
        problems.append(f"library.json's license is {json.dumps(manifest.get('license'))}, not {license_} (init.library.license, else the plugin's)")
    if manifest.get("source") != source:
        problems.append(f"library.json's source is {json.dumps(manifest.get('source'))}, not the plugin's {json.dumps(source)}")
    listed = manifest.get("files")
    if not isinstance(listed, dict) or listed.keys() != data.keys():
        problems.append("library.json's `files` does not list exactly the archive's entries besides library.json and library.sig")
    else:
        problems += [f"entry {n} is not the SHA-256 library.json lists" for n, body in data.items() if hashlib.sha256(body).hexdigest() != listed[n]]
    if "LicenseRef-" in license_ and not any(n.startswith("Licenses/") for n in data):
        problems.append(f"library.json's license {license_} names a LicenseRef- and the archive has no text under Licenses/ (the rule for every package)")
    items = manifest.get("items")
    if not (isinstance(items, list) and len(items) == 1 and isinstance(items[0], dict) and items[0].get("kind") == "sound" and items[0].get("id") == sound):
        return problems + [f"library.json's items are not exactly the one Sound {sound} (init.sound)"], manifest
    folder = items[0].get("path")
    document = f"{folder}/sound.json" if isinstance(folder, str) else None
    if document not in data or len(data[document]) > MAX_SOUND:
        return problems + [f"the Sound {sound} has no sound.json at its path {folder}"], manifest
    try:
        nodes = json.loads(data[document]).get("nodes")
        nodes = list(nodes.values()) if isinstance(nodes, dict) else nodes
        plays = [(n["plugin"]["id"], n["plugin"]["version"]) for n in nodes]
    except (ValueError, AttributeError, KeyError, TypeError):
        return problems + [f"{document} is not a Sound whose nodes name their plugins"], manifest
    have = {p["id"]: p["version"] for p in plugins}
    for plugin_id, version in plays:
        if plugin_id not in have:
            problems.append(f"the Sound {sound} plays {plugin_id}, which this release does not have")
        elif version != have[plugin_id]:
            problems.append(f"the Sound {sound} was saved with {plugin_id} {version}, and this release's is {have[plugin_id]}: a host opens a Sound only with the version it was saved with")
    if not any(plugin_id in have for plugin_id, _ in plays):
        problems.append(f"the Sound {sound} plays none of this release's plugins")
    return problems, manifest


def init_exception(packages, package, manifest, init_path, held, publisher, accept, problems):
    """The init library exception (README.md): the conditions on the library a release's `init`
    names, as a plan entry for signing it with the plugin; None, with the problems added, when any
    fails. `held` is the id its archive declares, None when it could not be read; `publisher` the
    plugin's, from its previous release."""
    library_id, version = f"{package['id']}.init", package["version"]
    before = len(problems)
    init = manifest["init"]
    if held != library_id:
        problems.append(f"the init library is {held}: a release may publish only its own init library, {library_id} (its package id and .init)")
        return None
    if {"id": library_id, "version": version} not in manifest.get("dependencies", {}).get("packages", []):
        problems.append(f"dependencies.packages must name {library_id} at {version}, this release's own version: a host opens a Sound only with the plugin version it was saved with, so the init library is this release's")
    here = packages / "packages" / library_id
    if (here / f"{version}.json").exists():
        problems.append(f"{library_id} {version} is here already: a version never changes once merged, and the import publishes the init library with its plugin")
    for path in sorted(here.glob("*.json")):
        owner = json.loads(path.read_bytes()).get("publisher")
        if owner != publisher:
            problems.append(f"{path} is published by {owner}, not {publisher}, the publisher of {package['id']}")
    license_ = init["library"].get("license", manifest["license"])
    last = max(here.glob("*.json"), key=lambda p: version_key(p.stem), default=None)
    if last is not None and json.loads(last.read_bytes()).get("license") != license_ and not accept:
        problems.append(f"the init library's license is not {last}'s ({json.loads(last.read_bytes()).get('license')} becomes {license_}; if that is meant, run again accepting a license change)")
    source = {"url": manifest["source"]["url"], "revision": manifest["source"]["revision"]}
    found, contents = init_archive_problems(init_path, library_id, manifest["plugins"], init["sound"], license_, source)
    problems += [f"{init['library']['file']} (init library) {p}" for p in found]
    if contents is not None and not found:
        needs = contents.get("dependencies", {}).get("packages", [])
        problems += [f"the init library {p}" for p in package_dependency_problems(packages, library_id, needs)]
    if len(problems) > before:
        return None
    return {
        "id": library_id, "version": version, "name": contents["name"], "publisher": publisher,
        "summary": f"The init Sound of {package['name']} {version}",
        "tags": ["init"], "license": license_, "source": source,
        "dependencies": contents.get("dependencies", {}).get("assets", []), "packageDependencies": contents.get("dependencies", {}).get("packages", []),
        "library": f"init/{init['library']['file']}", "sha256": init["library"]["sha256"], "sound": init["sound"],
        "archive": f"out/{library_id}-{version}.olib",
    }


def normalized(manifest):
    """A manifest with the defaults oscairn-sign's writer fills in for what a reader treats as absent
    (no assets or dependencies, an item without signatures), so a file that left them out compares
    equal to its re-signed archive."""
    m = dict(manifest)
    for field, default in (("assets", None), ("dependencies", {"assets": []})):
        if m.get(field) is None:
            m[field] = default
    m["items"] = [{"signatures": [], **i} if isinstance(i, dict) else i for i in m.get("items", [])]
    return m


def derived_problems(original, signed):
    """The archive signed and published must be the file plugin-check played, re-signed: the same
    entries with the same bytes (library.sig aside), and a manifest with the same content."""
    problems = []
    with zipfile.ZipFile(original) as a, zipfile.ZipFile(signed) as b:
        before = {i.filename: i for i in a.infolist() if i.filename not in ("library.json", "library.sig")}
        after = {i.filename: i for i in b.infolist() if i.filename not in ("library.json", "library.sig")}
        for name in sorted(set(before) ^ set(after)):
            problems.append(f"{name} is {'only in the file that was checked' if name in before else 'only in the archive signed'}")
        for name in sorted(set(before) & set(after)):
            if before[name].is_dir() or after[name].is_dir():
                continue
            if before[name].file_size != after[name].file_size or a.read(before[name]) != b.read(after[name]):
                problems.append(f"{name} differs between the file that was checked and the archive signed")
        if normalized(json.loads(a.read("library.json"))) != normalized(json.loads(b.read("library.json"))):
            problems.append("library.json differs between the file that was checked and the archive signed")
    return problems


# verify --------------------------------------------------------------------------------------

def where_from(args):
    """The release's download address, and the fork it is on (None for --from)."""
    if args.release:
        m = re.fullmatch(r"https://github\.com/(oscairn/[A-Za-z0-9._-]+)/releases/tag/((?:oscairn-)?v[0-9]+\.[0-9]+\.[0-9]+)", args.release)
        if not m:
            raise Refused(f"{args.release} is not https://github.com/oscairn/<fork>/releases/tag/[oscairn-]vX.Y.Z")
        args.repo, args.tag = m.groups()
    if args.repo:
        if not re.fullmatch(r"oscairn/[A-Za-z0-9._-]+", args.repo) or not re.fullmatch(r"(?:oscairn-)?v[0-9]+\.[0-9]+\.[0-9]+", args.tag or ""):
            raise Refused("--repo is oscairn/<fork> and --tag vX.Y.Z or oscairn-vX.Y.Z")
        return f"https://github.com/{args.repo}/releases/download/{args.tag}/", f"https://github.com/{args.repo}"
    if not args.from_:
        raise Refused("give --release, --repo and --tag, or --from")
    return args.from_.rstrip("/") + "/", None


def resolve_tag(url, tag, git_base):
    """The commit the tag names in the fork, peeled when annotated."""
    remote = git_base.rstrip("/") + "/" + url[len("https://github.com/"):]
    run = subprocess.run(["git", "ls-remote", "--tags", remote, f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}"],
                         capture_output=True, text=True, timeout=120)
    if run.returncode != 0:
        raise Refused(f"cannot read {url}'s tags: {run.stderr.strip()}")
    refs = dict(reversed(line.split("\t")) for line in run.stdout.splitlines() if "\t" in line)
    return refs.get(f"refs/tags/{tag}^{{}}") or refs.get(f"refs/tags/{tag}")


def commands(plan, key, tool):
    """The oscairn-sign commands that sign the candidate, run in it: each build, then the archive."""
    out = []
    plugins = ",".join(f"{p['id']}@{p['version']}" for p in plan["plugins"])
    for b in plan["builds"]:
        out.append([tool, "build", key, "clap", plugins, plan["version"], b["platform"], b["path"]])
    package = [tool, "plugin-package", "--key", key, "--license", plan["license"]]
    for f in plan["licenseFiles"]:
        package += ["--license-file", f]
    package += ["--source", plan["source"]["url"], plan["source"]["revision"]]
    for d in plan["dependencies"]:
        package += ["--asset-dependency", d["id"], d["version"]]
    for d in plan.get("packageDependencies", []):
        package += ["--package-dependency", d["id"], d["version"]]
    for p in plan["plugins"]:
        package += ["--plugin", f"{p['id']}@{p['version']}", p["name"]]
    package += [plan["archive"], "clap", plan["id"], plan["version"], plan["name"]]
    package += [f"{b['platform']}={b['path']}" for b in plan["builds"]]
    out.append(package)
    if plan.get("initPackage"):
        # The init library: the file plugin-check played, copied and signed whole by the same key.
        init = plan["initPackage"]
        out += [["cp", init["library"], init["archive"]], [tool, "archive", key, init["archive"]]]
    return out


def verify(args):
    base, fork = where_from(args)
    out = Path(args.out)
    if out.exists():
        raise Refused(f"{out} exists already")
    out.mkdir(parents=True)
    problems = []
    download(base + MANIFEST, out / MANIFEST, MAX_MANIFEST)
    manifest = json.loads((out / MANIFEST).read_bytes())
    problems += validate(manifest, json.loads(SCHEMA.read_bytes()))
    if problems:
        raise Refused("the manifest is not valid:\n  " + "\n  ".join(problems))
    package, source = manifest["package"], manifest["source"]
    for field, values in (("builds' platforms", [b["platform"] for b in manifest["builds"]]), ("builds' files", [b["file"] for b in manifest["builds"]]),
                          ("plugins' IDs", [p["id"] for p in manifest["plugins"]]), ("license files' names", [f["file"] for f in manifest["licenseFiles"]])):
        if len(set(values)) != len(values):
            problems.append(f"the {field} are not each different")
    # A build for a platform with no row in PLATFORMS cannot be played and checked here, so it is
    # left out: not fetched, not in the archive, not in the record. The manifest is not signed (each
    # build has its own SHA-256, and the archive is made and signed from the builds kept), so the
    # builds kept check the same either way. A release with no build to keep is refused.
    builds = [b for b in manifest["builds"] if b["platform"] in PLATFORMS]
    left_out = [b["platform"] for b in manifest["builds"] if b["platform"] not in PLATFORMS]
    if not builds:
        problems.append(f"no build to import: the release has builds for {', '.join(left_out)} only, and PLATFORMS has no row for them (no Oscairn host to check them on)")
    if source["url"] not in FORKS | FIRST_PARTY:
        problems.append(f"source.url {source['url']} is not one of Oscairn's plugin repositories: {sorted(FORKS | FIRST_PARTY)}")
    if fork and source["url"] != fork:
        problems.append(f"source.url {source['url']} is not {fork}, which holds the release")
    if source["tag"] not in ("v" + package["version"], "oscairn-v" + package["version"]) or (args.tag and source["tag"] != args.tag):
        problems.append(f"source.tag {source['tag']} is not v{package['version']} or oscairn-v{package['version']}" + (f" and {args.tag}" if args.tag else ""))
    packages = Path(args.packages)
    if any(e.get("id") == package["id"] and e.get("version") == package["version"] for e in removed_versions(packages)):
        problems.append(f"{package['id']} {package['version']} was removed (removed.json): a removed version is never published again; publish a new version")
    if (packages / "packages" / package["id"] / f"{package['version']}.json").exists():
        problems.append(f"{package['id']} {package['version']} is here already: a version never changes once merged")
    has_records = any((packages / "packages" / package["id"]).glob("*.json"))
    if args.first_release and has_records:
        problems.append(f"--first-release: {package['id']} has a release here already, so this is not its first; run again without it")
    if not has_records and any(e.get("id") == package["id"] for e in removed_versions(packages)):
        problems.append(f"{package['id']} has no release here, and removed.json lists it: a removed package id is not imported again as a new one")
    elif not has_records and not args.first_release:
        problems.append(f"no release of {package['id']} here yet: a first release of a new package id is imported only when asked for"
                        " (--first-release; the workflow's first-release input), or is submitted by hand (README.md)")
    if args.first_release and not has_records and not (packages / "publishers" / f"{OWN_PUBLISHER}.json").exists():
        problems.append(f"publishers/{OWN_PUBLISHER}.json is not here: a first release is {OWN_PUBLISHER}'s")
    package_needs = manifest.get("dependencies", {}).get("packages", [])
    init = manifest.get("init")
    arriving = {f"{package['id']}.init"} if init else set()  # the init library arrives in this run, so it need not be here yet
    problems += package_dependency_problems(packages, package["id"], package_needs, arriving)
    if init and init["library"]["size"] > INIT_MAX_BYTES:
        problems.append(f"init.library.size {init['library']['size']} is over the {INIT_MAX_BYTES} bytes an init library published with its plugin may be")
    if init and init["library"]["file"] in {MANIFEST, LICENSES, *(b["file"] for b in manifest["builds"])}:
        problems.append(f"init.library.file {init['library']['file']} is also another file of the release")
    if problems:
        raise Refused("\n".join(problems))
    commit = resolve_tag(source["url"], source["tag"], args.git_base)
    if commit != source["revision"]:
        problems.append(f"the tag {source['tag']} in {source['url']} is {commit or 'not there'}, not source.revision {source['revision']}")

    # Every file, by its SHA-256 and size; each build's libraries.
    for b in builds:
        path = out / "builds" / b["platform"] / b["file"]
        path.parent.mkdir(parents=True)
        download(base + b["file"], path, b["size"])
        if path.stat().st_size != b["size"] or sha256_file(path) != b["sha256"]:
            problems.append(f"{b['file']} is not the size and SHA-256 the manifest gives")
            continue
        problems += [f"{b['platform']} build {b['file']} {p}" for p in library_problems(path, b["platform"])]
    init_path, held = None, None
    if init and init["library"]["size"] <= INIT_MAX_BYTES:
        # The init library, fetched and checked as a build is; it is only played at plugin-check and
        # is not part of the package (a library package of its own, which dependencies.packages names).
        library = init["library"]
        init_path = out / "init" / library["file"]
        init_path.parent.mkdir()
        download(base + library["file"], init_path, library["size"])
        if init_path.stat().st_size != library["size"] or sha256_file(init_path) != library["sha256"]:
            problems.append(f"{library['file']} (init library) is not the size and SHA-256 the manifest gives")
        else:
            try:
                with zipfile.ZipFile(init_path) as z:
                    if len(z.namelist()) != len(set(z.namelist())):
                        raise ValueError("it has an entry twice (a second library.json would be a second library id)")
                    if z.getinfo("library.json").file_size > MAX_MANIFEST:
                        raise ValueError("its library.json is over 1 MiB")
                    held = json.loads(z.read("library.json")).get("id")
            except (zipfile.BadZipFile, KeyError, ValueError, AttributeError) as e:
                problems.append(f"{library['file']} (init library) is not a library archive: {e}")
            else:
                if held not in [n["id"] for n in package_needs]:
                    problems.append(f"the init library is {held}, which the manifest's dependencies.packages does not name: the package must need its init library")
    download(base + LICENSES, out / LICENSES, 64 << 20)
    with tarfile.open(out / LICENSES, "r:gz") as tar:
        names = {m.name.removeprefix("./"): m for m in tar.getmembers()}
        (out / "licenses").mkdir()
        for f in manifest["licenseFiles"]:
            member = names.get(f["file"])
            if member is None or not member.isfile() or member.size > MAX_LICENSE:
                problems.append(f"{LICENSES} has no file {f['file']}")
                continue
            data = tar.extractfile(member).read()
            if hashlib.sha256(data).hexdigest() != f["sha256"]:
                problems.append(f"{f['file']} is not the SHA-256 the manifest gives")
            (out / f["file"]).write_bytes(data)

    # The license, against the package's last release.
    first = args.first_release
    if first:
        path, previous = None, {"publisher": OWN_PUBLISHER, "tags": [], "channel": "stable"}
    else:
        path, previous = previous_release(packages, package["id"])
    init_plan = None
    if init and init_path is not None and init_path.exists() and init_path.stat().st_size == init["library"]["size"] \
            and sha256_file(init_path) == init["library"]["sha256"]:
        init_plan = init_exception(packages, package, manifest, init_path, held, previous["publisher"], args.accept_license_change, problems)
        if init_plan:
            init_plan["channel"] = previous.get("channel", "stable")
    now = {Path(f["file"]).name: f["sha256"] for f in manifest["licenseFiles"]}
    if first:
        # Nothing to compare with: whoever approves the signing accepts this license and source for the first time.
        before, changes = now, []
        shown = (f"first release of a new package id: {package['id']} {package['version']} ({package['name']})\n"
                 f"publisher: {OWN_PUBLISHER}\nlicense: {manifest['license']}\n"
                 f"license files: {', '.join(sorted(now)) or 'none'}\n"
                 f"source: {source['url']} {source['tag']} ({source['revision']})\n")
    else:
        with tempfile.TemporaryDirectory() as temp:
            before = previous_license_files(path, previous, Path(temp))
        changes = [f"license {previous.get('license')} becomes {manifest['license']}"] if manifest["license"] != previous.get("license") else []
    changes += [f"license file {n} is new" for n in sorted(set(now) - set(before))]
    changes += [f"license file {n} is gone" for n in sorted(set(before) - set(now))]
    changes += [f"license file {n} changed" for n in sorted(set(now) & set(before)) if now[n] != before[n]]
    if changes and args.accept_license_change:
        print(f"accepted, against {path}:\n  " + "\n  ".join(changes))
    elif changes:
        problems.append(f"the license is not {path}'s (if that is meant, run again accepting a license change):\n  " + "\n  ".join(changes))
    if problems:
        raise Refused("\n".join(problems))
    if first:
        (out / "first-release.txt").write_text(shown)
        print(shown, end="")

    publisher = previous["publisher"]
    plan = {
        "id": package["id"], "version": package["version"], "name": package["name"], "summary": package["summary"],
        "publisher": publisher, "tags": previous.get("tags", []), "channel": previous.get("channel", "stable"),
        "license": manifest["license"], "licenseFiles": [f["file"] for f in manifest["licenseFiles"]],
        "source": {"url": source["url"], "revision": source["revision"]}, "tag": source["tag"],
        "dependencies": manifest.get("dependencies", {}).get("assets", []),
        "packageDependencies": package_needs,
        **({"init": {"library": f"init/{init['library']['file']}", "sound": init["sound"]}} if init else {}),
        **({"initPackage": init_plan} if init_plan else {}),
        "plugins": manifest["plugins"],
        "builds": [{"platform": b["platform"], "path": f"builds/{b['platform']}/{b['file']}"} for b in builds],
        "notImported": left_out,
        **({"firstRelease": True} if first else {}),
        "archive": f"out/{package['id']}-{package['version']}.olib",
    }
    (out / "plan.json").write_text(json.dumps(plan, indent=2) + "\n")
    (out / "matrix.json").write_text(json.dumps([{"platform": b["platform"], "runner": PLATFORMS[b["platform"]]["runner"]} for b in builds]))
    notice = "".join(f"{platform} build not imported: no Oscairn host for that platform yet\n" for platform in left_out)
    (out / "notice.txt").write_text(notice)
    quoted = lambda argv: " ".join(a if a in ('"$key"', '"$sign"') else shlex.quote(a) for a in argv)
    (out / "sign.sh").write_text(
        "#!/bin/sh\n"
        f"# Signs {package['id']} {package['version']}, checked by import-release.py verify, with the publisher's key.\n"
        "#   sh sign.sh <publisher key file> <oscairn-sign>\n"
        "# then, in a checkout of oscairn/packages:\n"
        "#   python3 scripts/import-release.py record --candidate <this folder> --oscairn-sign <oscairn-sign>\n"
        "set -eu\nkey=$(realpath \"$1\")\ncase $2 in */*) sign=$(realpath \"$2\") ;; *) sign=$(command -v \"$2\") ;; esac\n"
        "cd \"$(dirname \"$0\")\"\nmkdir -p out\n"
        + "".join(quoted(c) + "\n" for c in commands(plan, '"$key"', '"$sign"')))
    print(f"verified {package['id']} {package['version']} from {source['url']} {source['tag']} ({source['revision']}): "
          + ", ".join(b["platform"] for b in builds))
    print(notice, end="")


# plugin-check, sign, record ------------------------------------------------------------------

def plugin_check(args):
    candidate = Path(args.candidate).resolve()
    plan = json.loads((candidate / "plan.json").read_bytes())
    build = next((b for b in plan["builds"] if b["platform"] == args.platform), None)
    if build is None:
        raise Refused(f"no {args.platform} build")
    problems = []
    for p in plan["plugins"]:
        # A fresh process for each, with a time limit; the runner (or your machine) bounds its memory.
        command = [args.plugin_check, str(candidate / build["path"]), p["id"], "synth" if p["kind"] == "synth" else "effect"]
        if plan.get("init"):  # an instrument that needs content: the init library's Sound makes its part
            command += ["--library", str(candidate / plan["init"]["library"]), "--sound", plan["init"]["sound"]]
        run = subprocess.run(command, capture_output=True, text=True, timeout=PLUGIN_CHECK_SECONDS)
        print(run.stdout + run.stderr)
        noted = "a note effect:" in run.stdout
        if run.returncode != 0:
            problems.append(f"oscairn-plugin-check refuses {p['id']} on {args.platform}")
        elif noted != (p["kind"] == "note-effect"):
            problems.append(f"{p['id']} is {'a' if noted else 'not a'} note effect to oscairn-plugin-check, and its kind is {p['kind']}")
    if problems:
        raise Refused("\n".join(problems))


def init_file_problems(candidate, plan):
    """The init library file in the candidate, read again as verify read it: the file plugin-check
    played must still be the one verify checked, and still pass every condition on its contents."""
    init = plan["initPackage"]
    file = candidate / init["library"]
    if not file.is_file() or sha256_file(file) != init["sha256"]:
        return [f"{init['library']} is not the SHA-256 verify checked"]
    problems, _ = init_archive_problems(file, init["id"], plan["plugins"], init["sound"], init["license"], init["source"])
    return [f"{init['library']} (init library) {p}" for p in problems]


def sign(args):
    candidate = Path(args.candidate).resolve()
    plan = json.loads((candidate / "plan.json").read_bytes())
    if plan.get("initPackage"):
        problems = init_file_problems(candidate, plan)
        if problems:
            raise Refused("\n".join(problems))
    (candidate / "out").mkdir(exist_ok=True)
    (candidate / plan["archive"]).unlink(missing_ok=True)
    if plan.get("initPackage"):
        (candidate / plan["initPackage"]["archive"]).unlink(missing_ok=True)
    for argv in commands(plan, str(Path(args.key).resolve()), args.oscairn_sign):
        run = subprocess.run(argv, cwd=candidate, capture_output=True, text=True, timeout=900)
        if run.returncode != 0:
            raise Refused(f"{Path(argv[0]).name} {argv[1]} failed: {run.stderr.strip()}")
        print(run.stdout.strip())
    if plan.get("initPackage"):
        problems = derived_problems(candidate / plan["initPackage"]["library"], candidate / plan["initPackage"]["archive"])
        if problems:
            (candidate / plan["initPackage"]["archive"]).unlink(missing_ok=True)
            raise Refused("the init library archive signed is not the file that was checked:\n" + "\n".join(problems))


def init_record_problems(candidate, plan, olib, key, tool):
    """The init library's archive, as `record` checks the plugin's: signed by the publisher's key and
    nothing else (no build), the file plugin-check played re-signed and not changed, and still
    within the exception's limits."""
    init = plan["initPackage"]
    problems = init_file_problems(candidate, plan)
    if not olib.is_file():
        return problems + [f"{init['archive']} is not there: sign it first"]
    run = subprocess.run([tool, "verify", str(olib)], capture_output=True, text=True, timeout=900)
    if run.returncode != 0:
        return problems + [f"the init library archive is refused: {run.stderr.strip()}"]
    lines = [line.split() for line in run.stdout.splitlines()]
    if ["archive", init["id"], key_id(key)] not in lines:
        problems.append(f"the init library archive is not {init['id']} signed by {plan['publisher']}'s key {key_id(key)}")
    problems += [" ".join(w) for w in lines if w[:1] in (["outside"], ["build"])]
    if olib.stat().st_size > INIT_MAX_BYTES:
        problems.append(f"the init library archive is {olib.stat().st_size} bytes, over the {INIT_MAX_BYTES} an init library may be")
    if not problems:
        problems += [f"the init library archive signed is not the file that was checked: {p}" for p in derived_problems(candidate / init["library"], olib)]
    if not problems:  # its manifest as the archive has it, which the record must equal (build-index.py)
        with zipfile.ZipFile(olib) as z:
            contents = json.loads(z.read("library.json"))
        if contents.get("license") != init["license"] or contents.get("source") != init["source"] or contents.get("id") != init["id"]:
            problems.append("the init library archive's id, license or source is not the plan's")
        init["packageDependencies"] = contents.get("dependencies", {}).get("packages", [])
        init["dependencies"] = contents.get("dependencies", {}).get("assets", [])
    return problems


def write_init_record(packages, init, olib):
    folder = packages / "packages" / init["id"]
    folder.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(olib, folder / f"{init['version']}.olib")
    r = {"format": "oscairn.repository-release", "version": 1, "id": init["id"], "release": init["version"], "publisher": init["publisher"],
         "summary": init["summary"], "license": init["license"], "tags": init["tags"], "channel": init["channel"], "platforms": [], "source": init["source"]}
    if init["dependencies"] or init["packageDependencies"]:
        r["dependencies"] = {"assets": init["dependencies"], **({"packages": init["packageDependencies"]} if init["packageDependencies"] else {})}
    r["archive"] = {"path": f"{init['version']}.olib", "sha256": sha256_file(olib), "size": olib.stat().st_size}
    r["yanked"] = None
    (folder / f"{init['version']}.json").write_text(json.dumps(r, indent=2, ensure_ascii=False) + "\n")
    print(f"recorded packages/{init['id']}/{init['version']}.json (the init library, published under the init exception)")


def record(args):
    candidate, packages = Path(args.candidate).resolve(), Path(args.packages)
    plan = json.loads((candidate / "plan.json").read_bytes())
    olib = candidate / plan["archive"]
    key = json.loads((packages / "publishers" / f"{plan['publisher']}.json").read_bytes())["key"]
    run = subprocess.run([args.oscairn_sign, "verify", str(olib)], capture_output=True, text=True, timeout=900)
    if run.returncode != 0:
        raise Refused(f"the archive is refused: {run.stderr.strip()}")
    lines = [line.split() for line in run.stdout.splitlines()]
    problems = [" ".join(w) for w in lines if w[:1] == ["outside"]]
    if ["archive", plan["id"], key_id(key)] not in lines:
        problems.append(f"the archive is not {plan['id']} signed by {plan['publisher']}'s key {key_id(key)}")
    builds = [w for w in lines if w[:1] == ["build"]]
    problems += [f"the {w[4]} build of {w[2]} is not signed by {plan['publisher']}'s key" for w in builds if key_id(key) not in w[5].split(",")]
    if len(builds) != len(plan["plugins"]) * len(plan["builds"]):
        problems.append(f"the archive holds {len(builds)} builds, not {len(plan['plugins']) * len(plan['builds'])}")
    size = olib.stat().st_size
    if size > MAX_BESIDE:
        # shortcut: archives go beside their records; one over 95 MiB needs an https:// url, by hand, until a fork's release can hold it
        problems.append(f"the archive is {size} bytes, too large to go beside its record: give it an https:// `url` by hand")
    init = plan.get("initPackage")
    init_olib = candidate / init["archive"] if init else None
    if init:
        problems += init_record_problems(candidate, plan, init_olib, key, args.oscairn_sign)
    if problems:
        raise Refused("\n".join(problems))
    if init:
        write_init_record(packages, init, init_olib)
    folder = packages / "packages" / plan["id"]
    folder.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(olib, folder / f"{plan['version']}.olib")
    r = {"format": "oscairn.repository-release", "version": 1, "id": plan["id"], "release": plan["version"], "publisher": plan["publisher"],
         "summary": plan["summary"], "license": plan["license"], "tags": plan["tags"], "channel": plan["channel"],
         "platforms": sorted(b["platform"] for b in plan["builds"]), "source": plan["source"]}
    if plan["dependencies"] or plan["packageDependencies"]:
        r["dependencies"] = {"assets": plan["dependencies"], **({"packages": plan["packageDependencies"]} if plan["packageDependencies"] else {})}
    r["archive"] = {"path": f"{plan['version']}.olib", "sha256": sha256_file(olib), "size": size}
    r["yanked"] = None
    (folder / f"{plan['version']}.json").write_text(json.dumps(r, indent=2, ensure_ascii=False) + "\n")
    print(f"recorded packages/{plan['id']}/{plan['version']}.json")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    steps = ap.add_subparsers(dest="step", required=True)
    v = steps.add_parser("verify")
    v.add_argument("--release", help="https://github.com/oscairn/<fork>/releases/tag/[oscairn-]vX.Y.Z")
    v.add_argument("--repo", help="oscairn/<fork>, with --tag")
    v.add_argument("--tag")
    v.add_argument("--from", dest="from_", help="the release's files at another address (a test's local server)")
    v.add_argument("--git-base", default="https://github.com/", help="where the forks' git repositories are (a test's own)")
    v.add_argument("--out", required=True)
    v.add_argument("--packages", default=".")
    v.add_argument("--accept-license-change", action="store_true")
    v.add_argument("--first-release", action="store_true", help="the package id has no release here: import its first (refused if it has one)")
    c = steps.add_parser("plugin-check")
    c.add_argument("--candidate", required=True)
    c.add_argument("--platform", required=True)
    c.add_argument("--plugin-check", required=True)
    s = steps.add_parser("sign")
    s.add_argument("--candidate", required=True)
    s.add_argument("--key", required=True)
    s.add_argument("--oscairn-sign", required=True)
    r = steps.add_parser("record")
    r.add_argument("--candidate", required=True)
    r.add_argument("--oscairn-sign", required=True)
    r.add_argument("--packages", default=".")
    args = ap.parse_args()
    try:
        {"verify": verify, "plugin-check": plugin_check, "sign": sign, "record": record}[args.step](args)
    except (Refused, OSError, ValueError, KeyError, subprocess.TimeoutExpired, tarfile.TarError, zipfile.BadZipFile) as e:
        print(f"import-release: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
