#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Imports a plugin release from one of Oscairn's forks (README.md, "Importing a release").

A fork's tag vX.Y.Z or oscairn-vX.Y.Z publishes a GitHub release holding oscairn-release.json
(the manifest, schemas/plugin-release.schema.json), one build file per platform and
licenses.tar.gz. Steps, each a command, as .github/workflows/import.yml runs them:

    import-release.py verify (--release <url> | --repo oscairn/<fork> --tag [oscairn-]vX.Y.Z | --from <url>)
                             --out <candidate> [--packages .] [--accept-license-change]
        Fetches the manifest and its files into <candidate>, and checks: the manifest against the
        schema; every file's SHA-256 and size; the source is an allowed Oscairn fork and the tag is
        source.revision there; each build's libraries (Mach-O and ELF read here, so on any
        machine); the license and license files are the package's last release's. Writes
        plan.json, matrix.json (a runner for each platform) and sign.sh (the commands that sign
        it, for signing by hand).
    import-release.py plugin-check --candidate <candidate> --platform <p> --plugin-check <tool>
        Plays each plugin of that platform's build through oscairn-plugin-check, on that platform.
    import-release.py sign --candidate <candidate> --key <key> --oscairn-sign <tool>
        What sign.sh does: signs each build and packs and signs the archive. Nothing else sees the key.
    import-release.py record --candidate <candidate> --oscairn-sign <tool> [--packages .]
        Checks the archive with `oscairn-sign verify` against the publisher's key and writes
        packages/<id>/<version>.json and .olib.

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
# One row per platform: the runner plugin-check runs on, and the binary a build must be
# (Mach-O CPU type or ELF machine). Another platform is another row.
PLATFORMS = {
    "macos-arm64": {"runner": "macos-15", "macho": 0x0100000C},
    "linux-arm64": {"runner": "ubuntu-24.04-arm", "elf": 183},
}
# Oscairn's Linux allowlist (oscairn docs/plugin-porting.md, "The Linux allowlist"): what every
# image carries, and the newest glibc and libstdc++ symbol versions every image has.
LINUX_LIBRARIES = {
    "libc.so.6", "libm.so.6", "libdl.so.2", "libpthread.so.0", "librt.so.1", "ld-linux-aarch64.so.1", "ld-linux-x86-64.so.2",
    "libgcc_s.so.1", "libstdc++.so.6", "libasound.so.2", "libX11.so.6", "libXext.so.6", "libXrandr.so.2", "libXinerama.so.1",
    "libXcursor.so.1", "libXrender.so.1", "libfreetype.so.6", "libfontconfig.so.1",
}
NEWEST = {"GLIBC_": (2, 44), "GLIBCXX_": (3, 4, 34)}
MAX_FILE = 1900 * 2**20  # as the schema's build size
MAX_MANIFEST = 1 << 20
MAX_LICENSE = 16 << 20
MAX_BESIDE = 95 * 2**20  # an archive beside its record: GitHub refuses files over 100 MiB
PLUGIN_CHECK_SECONDS = 600


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


# The previous release ------------------------------------------------------------------------

def previous_release(packages, package_id):
    records = sorted((packages / "packages" / package_id).glob("*.json"), key=lambda p: version_key(p.stem))
    if not records:
        raise Refused(f"no release of {package_id} here yet: its first release is submitted by hand (README.md)")
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
    for p in plan["plugins"]:
        package += ["--plugin", f"{p['id']}@{p['version']}", p["name"]]
    package += [plan["archive"], "clap", plan["id"], plan["version"], plan["name"]]
    package += [f"{b['platform']}={b['path']}" for b in plan["builds"]]
    return out + [package]


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
    for b in manifest["builds"]:
        if b["platform"] not in PLATFORMS:
            problems.append(f"no runner for {b['platform']}: add a row to PLATFORMS in import-release.py")
    if source["url"] not in FORKS:
        problems.append(f"source.url {source['url']} is not one of Oscairn's forks: {sorted(FORKS)}")
    if fork and source["url"] != fork:
        problems.append(f"source.url {source['url']} is not {fork}, which holds the release")
    if source["tag"] not in ("v" + package["version"], "oscairn-v" + package["version"]) or (args.tag and source["tag"] != args.tag):
        problems.append(f"source.tag {source['tag']} is not v{package['version']} or oscairn-v{package['version']}" + (f" and {args.tag}" if args.tag else ""))
    packages = Path(args.packages)
    if (packages / "packages" / package["id"] / f"{package['version']}.json").exists():
        problems.append(f"{package['id']} {package['version']} is here already: a version never changes once merged")
    if problems:
        raise Refused("\n".join(problems))
    commit = resolve_tag(source["url"], source["tag"], args.git_base)
    if commit != source["revision"]:
        problems.append(f"the tag {source['tag']} in {source['url']} is {commit or 'not there'}, not source.revision {source['revision']}")

    # Every file, by its SHA-256 and size; each build's libraries.
    for b in manifest["builds"]:
        path = out / "builds" / b["platform"] / b["file"]
        path.parent.mkdir(parents=True)
        download(base + b["file"], path, b["size"])
        if path.stat().st_size != b["size"] or sha256_file(path) != b["sha256"]:
            problems.append(f"{b['file']} is not the size and SHA-256 the manifest gives")
            continue
        problems += [f"{b['platform']} build {b['file']} {p}" for p in library_problems(path, b["platform"])]
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
    path, previous = previous_release(packages, package["id"])
    with tempfile.TemporaryDirectory() as temp:
        before = previous_license_files(path, previous, Path(temp))
    now = {Path(f["file"]).name: f["sha256"] for f in manifest["licenseFiles"]}
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

    publisher = previous["publisher"]
    plan = {
        "id": package["id"], "version": package["version"], "name": package["name"], "summary": package["summary"],
        "publisher": publisher, "tags": previous.get("tags", []), "channel": previous.get("channel", "stable"),
        "license": manifest["license"], "licenseFiles": [f["file"] for f in manifest["licenseFiles"]],
        "source": {"url": source["url"], "revision": source["revision"]}, "tag": source["tag"],
        "dependencies": manifest.get("dependencies", {}).get("assets", []),
        "plugins": manifest["plugins"],
        "builds": [{"platform": b["platform"], "path": f"builds/{b['platform']}/{b['file']}"} for b in manifest["builds"]],
        "archive": f"out/{package['id']}-{package['version']}.olib",
    }
    (out / "plan.json").write_text(json.dumps(plan, indent=2) + "\n")
    (out / "matrix.json").write_text(json.dumps([{"platform": b["platform"], "runner": PLATFORMS[b["platform"]]["runner"]} for b in manifest["builds"]]))
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
          + ", ".join(b["platform"] for b in manifest["builds"]))


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
        run = subprocess.run([args.plugin_check, str(candidate / build["path"]), p["id"], "synth" if p["kind"] == "synth" else "effect"],
                             capture_output=True, text=True, timeout=PLUGIN_CHECK_SECONDS)
        print(run.stdout + run.stderr)
        noted = "a note effect:" in run.stdout
        if run.returncode != 0:
            problems.append(f"oscairn-plugin-check refuses {p['id']} on {args.platform}")
        elif noted != (p["kind"] == "note-effect"):
            problems.append(f"{p['id']} is {'a' if noted else 'not a'} note effect to oscairn-plugin-check, and its kind is {p['kind']}")
    if problems:
        raise Refused("\n".join(problems))


def sign(args):
    candidate = Path(args.candidate).resolve()
    plan = json.loads((candidate / "plan.json").read_bytes())
    (candidate / "out").mkdir(exist_ok=True)
    (candidate / plan["archive"]).unlink(missing_ok=True)
    for argv in commands(plan, str(Path(args.key).resolve()), args.oscairn_sign):
        run = subprocess.run(argv, cwd=candidate, capture_output=True, text=True, timeout=900)
        if run.returncode != 0:
            raise Refused(f"oscairn-sign {argv[1]} failed: {run.stderr.strip()}")
        print(run.stdout.strip())


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
    if problems:
        raise Refused("\n".join(problems))
    folder = packages / "packages" / plan["id"]
    shutil.copyfile(olib, folder / f"{plan['version']}.olib")
    r = {"format": "oscairn.repository-release", "version": 1, "id": plan["id"], "release": plan["version"], "publisher": plan["publisher"],
         "summary": plan["summary"], "license": plan["license"], "tags": plan["tags"], "channel": plan["channel"],
         "platforms": sorted(b["platform"] for b in plan["builds"]), "source": plan["source"]}
    if plan["dependencies"]:
        r["dependencies"] = {"assets": plan["dependencies"]}
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
