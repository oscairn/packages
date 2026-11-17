#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""import-release.py end to end, on a release made here from Oscairn Arpeggiator 0.1.4's macOS build.

usage: test_import_release.py <oscairn-sign> [<oscairn-plugin-check>]

Serves a fake release (manifest, build, licenses.tar.gz) from a local folder over HTTP, with a
local git repository for the fork's tag, and imports it as org.oscairn.arpeggiator 0.1.5: verify,
plugin-check (when given the tool, on a Mac), sign with a throwaway key made here, both by
import-release.py sign and by the candidate's sign.sh, and record; the archive must pass
`oscairn-sign verify` and build-index.py --check. Then a tampered hash, a changed license file,
a macOS build with an LC_RPATH, a Linux build with a RUNPATH and too new a glibc, and a tag that
is not the manifest's revision are each refused, as is a tag not vX.Y.Z or oscairn-vX.Y.Z of the
package's version. Dependencies on other packages (dependencies.packages: present, missing, yanked,
itself, a cycle) and an init library (fetched, verified, passed to the check as --library and --sound
through a stand-in for oscairn-plugin-check, then signed and recorded with its plugin under the init
library exception, with each condition and hostile archives refused) are covered too, as are unknown keys. The key lives only
in a temporary folder.
"""
import functools
import hashlib
import http.server
import importlib.util
import io
import json
import platform
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import threading
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
SCRIPT = HERE / "import-release.py"
PREVIOUS = REPO / "packages" / "org.oscairn.arpeggiator"
failures = 0


def check(ok, what):
    global failures
    print(("ok   " if ok else "FAIL ") + what)
    failures += not ok


def run(*command, cwd=None):
    return subprocess.run([str(c) for c in command], capture_output=True, text=True, timeout=600, cwd=cwd)


def macho_with_rpath():
    """A 64-bit arm64 Mach-O bundle loading libSystem, with an LC_RPATH to Homebrew."""
    commands = b""
    for cmd, at, name in ((0xC, 24, b"/usr/lib/libSystem.B.dylib"), (0x8000001C, 12, b"/opt/homebrew/lib")):
        size = (at + len(name) + 1 + 7) & ~7
        commands += struct.pack("<III", cmd, size, at) + bytes(at - 12) + name + bytes(size - at - len(name))
    return struct.pack("<IIIIIIII", 0xFEEDFACF, 0x0100000C, 0, 8, 2, len(commands), 0, 0) + commands


def elf_too_new():
    """A 64-bit aarch64 ELF needing libc.so.6, with a RUNPATH, and needing GLIBC_2.45."""
    strings = b"\0libc.so.6\0/opt/lib\0GLIBC_2.45\0"
    dyn_at, str_at = 64 + 2 * 56, 64 + 2 * 56 + 7 * 16
    need_at = str_at + len(strings)
    dynamic = [(1, 1), (29, 11), (5, str_at), (10, len(strings)), (0x6FFFFFFE, need_at), (0x6FFFFFFF, 1), (0, 0)]
    need = struct.pack("<HHIII", 1, 1, 1, 16, 0) + struct.pack("<IHHII", 0, 0, 2, 20, 0)
    total = need_at + len(need)
    head = b"\x7fELF" + bytes([2, 1, 1]) + bytes(9) + struct.pack("<HHIQQQIHHHHHH", 3, 183, 1, 0, 64, 0, 0, 64, 56, 2, 64, 0, 0)
    segments = struct.pack("<IIQQQQQQ", 1, 4, 0, 0, 0, total, total, 0) + struct.pack("<IIQQQQQQ", 2, 4, dyn_at, dyn_at, dyn_at, 7 * 16, 7 * 16, 0)
    return head + segments + b"".join(struct.pack("<QQ", t, v) for t, v in dynamic) + strings + need


def main(tool, plugin_check=None):
    temp = Path(tempfile.mkdtemp(prefix="oscairn-import-test-"))
    try:
        out = run(tool, "key", temp / "keys" / "test.key")
        assert out.returncode == 0, out.stderr
        _, test_id, test_key = out.stdout.split()

        # What the last release holds: its macOS build and license files.
        with zipfile.ZipFile(PREVIOUS / "0.1.4.olib") as z:
            names = z.namelist()
            bundle = z.read(next(n for n in names if n.endswith("/macos-arm64/oscairn-arpeggiator.clap")))
            licenses = {n[len("Licenses/"):]: z.read(n) for n in names if n.startswith("Licenses/")}

        # The fork: a git repository with the release's tag, annotated, in both forms.
        fork = temp / "git" / "oscairn" / "arpeggiator"
        fork.mkdir(parents=True)
        for command in (["init", "-q"], ["-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "--allow-empty", "-m", "release"],
                        ["-c", "user.name=t", "-c", "user.email=t@example.com", "tag", "-a", "-m", "v0.1.5", "v0.1.5"],
                        ["-c", "user.name=t", "-c", "user.email=t@example.com", "tag", "-a", "-m", "oscairn-v0.1.5", "oscairn-v0.1.5"]):
            assert run("git", "-C", fork, *command).returncode == 0
        revision = run("git", "-C", fork, "rev-parse", "HEAD").stdout.strip()

        # This repository as the import sees it: the publisher with the test key, and the last release.
        packages = temp / "packages"
        (packages / "publishers").mkdir(parents=True)
        (packages / "packages").mkdir()
        # Only 0.1.4, the release the fixture is made from: a newer record here (its license files named otherwise) must not change what the test compares with.
        shutil.copytree(PREVIOUS, packages / "packages" / PREVIOUS.name, ignore=lambda d, names: [n for n in names if not n.startswith("0.1.4.")])
        publisher = {"format": "oscairn.repository-publisher", "version": 1, "id": "org.oscairn", "name": "Oscairn", "key": test_key,
                     "verification": "https://oscairn.org/.well-known/oscairn-publisher.json"}
        (packages / "publishers" / "org.oscairn.json").write_text(json.dumps(publisher))

        # The release, served from a folder; each case rewrites it.
        served = temp / "release"
        served.mkdir()

        def release(build=bundle, build_platform="macos-arm64", license_texts=licenses, change=lambda m: None):
            for f in served.iterdir():
                f.unlink()
            name = f"oscairn-arpeggiator-{build_platform}.clap"
            (served / name).write_bytes(build)
            with tarfile.open(served / "licenses.tar.gz", "w:gz") as tar:
                for n, data in license_texts.items():
                    info = tarfile.TarInfo(f"licenses/{n}")
                    info.size = len(data)
                    tar.addfile(info, io.BytesIO(data))
            manifest = {
                "format": "oscairn.plugin-release", "version": 1,
                "package": {"id": "org.oscairn.arpeggiator", "version": "0.1.5", "name": "Oscairn Arpeggiator",
                            "summary": "Oscairn's MIDI arpeggiator (a note effect), with Montage-style phrases, recording and tempo sync."},
                "plugins": [{"format": "clap", "id": "org.oscairn.arpeggiator", "version": "0.1.4", "name": "Oscairn Arpeggiator", "kind": "note-effect"}],
                "builds": [{"platform": build_platform, "file": name, "sha256": hashlib.sha256(build).hexdigest(), "size": len(build)}],
                "source": {"url": "https://github.com/oscairn/arpeggiator", "revision": revision, "tag": "v0.1.5"},
                "license": "MIT",
                "licenseFiles": [{"file": f"licenses/{n}", "sha256": hashlib.sha256(d).hexdigest()} for n, d in license_texts.items()],
                "dependencies": {"assets": []},
                "clap": "1.2.10", "oscairnClap": "7b5c4e0",
            }
            change(manifest)
            (served / "oscairn-release.json").write_text(json.dumps(manifest))

        handler = functools.partial(type("Quiet", (http.server.SimpleHTTPRequestHandler,), {"log_message": lambda *a: None}), directory=str(served))
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}/"
        cases = iter(range(1000))

        def verify(*extra):
            candidate = temp / f"candidate-{next(cases)}"
            result = run(sys.executable, "-I", SCRIPT, "verify", "--from", base, "--git-base", temp / "git", "--out", candidate, "--packages", packages, *extra)
            return candidate, result

        # The release as it should be.
        release()
        candidate, result = verify()
        check(result.returncode == 0, "a good release verifies: " + result.stdout + result.stderr)
        plan = json.loads((candidate / "plan.json").read_bytes())
        check(json.loads((candidate / "matrix.json").read_bytes()) == [{"platform": "macos-arm64", "runner": "macos-15"}], "it runs plugin-check on macos-15")
        if plugin_check and sys.platform == "darwin" and platform.machine() == "arm64":
            result = run(sys.executable, "-I", SCRIPT, "plugin-check", "--candidate", candidate, "--platform", "macos-arm64", "--plugin-check", plugin_check)
            check(result.returncode == 0, "oscairn-plugin-check plays it as a note effect: " + result.stdout[-400:] + result.stderr)
        else:
            print("skip plugin-check: needs oscairn-plugin-check, on an arm64 Mac")

        by_hand = temp / "by-hand"
        shutil.copytree(candidate, by_hand)
        result = run(sys.executable, "-I", SCRIPT, "sign", "--candidate", candidate, "--key", temp / "keys" / "test.key", "--oscairn-sign", tool)
        check(result.returncode == 0, "it signs: " + result.stdout + result.stderr)
        result = run(sys.executable, "-I", SCRIPT, "record", "--candidate", candidate, "--oscairn-sign", tool, "--packages", packages)
        check(result.returncode == 0, "it records: " + result.stdout + result.stderr)
        made = packages / "packages" / "org.oscairn.arpeggiator"
        result = run(tool, "verify", made / "0.1.5.olib")
        check(result.returncode == 0 and f"archive org.oscairn.arpeggiator {test_id}" in result.stdout
              and f"build clap org.oscairn.arpeggiator 0.1.4 macos-arm64 {test_id}" in result.stdout and "outside" not in result.stdout,
              "the .olib verifies, signed by the test key: " + result.stdout + result.stderr)
        r = json.loads((made / "0.1.5.json").read_bytes())
        check(r["source"] == {"url": "https://github.com/oscairn/arpeggiator", "revision": revision} and r["license"] == "MIT"
              and r["tags"] == ["note-effect", "arpeggiator"] and r["platforms"] == ["macos-arm64"], "the record has the release's facts and the last one's tags")

        # check.yml's own check on a repository holding the publisher and the new release.
        checked = temp / "checked"
        (checked / "packages" / "org.oscairn.arpeggiator").mkdir(parents=True)
        shutil.copytree(packages / "publishers", checked / "publishers")
        shutil.copy(REPO / "repository-config.json", checked)
        shutil.copy(REPO / "revoked.json", checked)
        for f in ("0.1.5.json", "0.1.5.olib"):
            shutil.copy(made / f, checked / "packages" / "org.oscairn.arpeggiator")
        result = run(sys.executable, "-I", HERE / "build-index.py", "--check", "--oscairn-sign", tool, "--repo", checked,
                     "--cache", temp / "cache", "--out", temp / "site", "--serial", "0")
        check(result.returncode == 0, "build-index.py --check passes it: " + result.stdout + result.stderr)

        # The other way: sign.sh, by hand, gives an archive that verifies the same way.
        result = run("sh", by_hand / "sign.sh", Path("keys") / "test.key", Path(tool).resolve(), cwd=temp)
        check(result.returncode == 0, "sign.sh signs: " + result.stdout + result.stderr)
        result = run(tool, "verify", by_hand / plan["archive"])
        check(result.returncode == 0 and f"archive org.oscairn.arpeggiator {test_id}" in result.stdout, "sign.sh's .olib verifies: " + result.stdout + result.stderr)

        # Refused.
        (made / "0.1.5.json").unlink()
        release(change=lambda m: m["builds"][0].update(sha256="0" * 64))
        _, result = verify()
        check(result.returncode == 1 and "not the size and SHA-256" in result.stderr, "a tampered hash is refused: " + result.stderr)

        changed = dict(licenses, **{"oscairn-arpeggiator-LICENSE": licenses["oscairn-arpeggiator-LICENSE"] + b"\nAnd one more condition.\n"})
        release(license_texts=changed)
        _, result = verify()
        check(result.returncode == 1 and "license file oscairn-arpeggiator-LICENSE changed" in result.stderr, "a changed license file is refused: " + result.stderr)
        _, result = verify("--accept-license-change")
        check(result.returncode == 0 and "changed" in result.stdout, "and accepted when asked: " + result.stdout + result.stderr)
        release(change=lambda m: m.update(license="GPL-3.0-or-later"))
        _, result = verify()
        check(result.returncode == 1 and "license MIT becomes GPL-3.0-or-later" in result.stderr, "a changed license is refused: " + result.stderr)

        for url, listed in (("https://github.com/oscairn/other", False), ("https://github.com/oscairn/va", True), ("https://github.com/oscairn/sampler", True), ("https://github.com/oscairn/fm", True)):
            release(change=lambda m: m["source"].update(url=url))
            _, result = verify()
            check(("is not one of Oscairn's plugin repositories" not in result.stderr) == listed, f"{url} is {'listed' if listed else 'refused'}: " + result.stderr)

        # A package whose new release comes from another first-party repository than its last one's
        # (the previous record's source is oscairn/arpeggiator): no check compares the two source URLs, so
        # it verifies; the publisher stays the last release's, and the license is still compared.
        moved = temp / "git" / "oscairn" / "sampler"
        moved.mkdir(parents=True)
        for command in (["init", "-q"], ["-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "--allow-empty", "-m", "release"],
                        ["-c", "user.name=t", "-c", "user.email=t@example.com", "tag", "-a", "-m", "v0.1.5", "v0.1.5"]):
            assert run("git", "-C", moved, *command).returncode == 0
        moved_revision = run("git", "-C", moved, "rev-parse", "HEAD").stdout.strip()
        release(change=lambda m: m["source"].update(url="https://github.com/oscairn/sampler", revision=moved_revision))
        candidate, result = verify()
        check(result.returncode == 0 and json.loads((candidate / "plan.json").read_bytes())["publisher"] == "org.oscairn"
              and json.loads((candidate / "plan.json").read_bytes())["source"]["url"] == "https://github.com/oscairn/sampler",
              "a new release from another first-party repository verifies, as the last release's publisher: " + result.stdout + result.stderr)
        release(change=lambda m: m["source"].update(url="https://github.com/oscairn/sampler", revision=moved_revision) or m.update(license="GPL-3.0-or-later"))
        _, result = verify()
        check(result.returncode == 1 and "license MIT becomes GPL-3.0-or-later" in result.stderr, "and a changed license is still refused there: " + result.stderr)

        # A platform with no row is left out, said so, and the rest is imported; with no other build it is refused.
        def with_windows(m):
            m["builds"].append({"platform": "windows-x86_64", "file": "oscairn-arpeggiator-windows-x86_64.clap", "sha256": "0" * 64, "size": 9})
        release(change=with_windows)  # its file is not served: a left-out build is never fetched
        candidate, result = verify()
        plan = json.loads((candidate / "plan.json").read_bytes())
        check(result.returncode == 0 and "windows-x86_64 build not imported: no Oscairn host for that platform yet" in result.stdout,
              "a build for a platform with no row is left out, with a notice: " + result.stdout + result.stderr)
        check([b["platform"] for b in plan["builds"]] == ["macos-arm64"] and plan["notImported"] == ["windows-x86_64"]
              and json.loads((candidate / "matrix.json").read_bytes()) == [{"platform": "macos-arm64", "runner": "macos-15"}]
              and "windows" not in (candidate / "sign.sh").read_text() and not (candidate / "builds" / "windows-x86_64").exists()
              and (candidate / "notice.txt").read_text().startswith("windows-x86_64 build not imported"),
              "the plan, matrix and sign.sh do not have it")
        check(not (candidate / "notice.txt").read_text() == "" and (candidate / "notice.txt").exists(), "notice.txt says so")
        release()
        candidate, result = verify()
        check((candidate / "notice.txt").read_text() == "" and json.loads((candidate / "plan.json").read_bytes())["notImported"] == []
              and "not imported" not in result.stdout, "a release of known platforms has no notice")

        def only_windows(m):
            m["builds"][0]["platform"] = "windows-x86_64"
        release(change=only_windows)
        _, result = verify()
        check(result.returncode == 1 and "no build to import" in result.stderr, "a release with no importable build is refused: " + result.stderr)

        release(build=macho_with_rpath())
        _, result = verify()
        check(result.returncode == 1 and "LC_RPATH, /opt/homebrew/lib" in result.stderr, "a macOS build with an rpath is refused: " + result.stderr)

        release(build=elf_too_new(), build_platform="linux-arm64")
        _, result = verify()
        check(result.returncode == 1 and "RUNPATH, /opt/lib" in result.stderr and "needs GLIBC_2.45" in result.stderr,
              "a Linux build with a RUNPATH and too new a glibc is refused: " + result.stderr)

        release(change=lambda m: m["source"].update(revision="1" * 40))
        _, result = verify()
        check(result.returncode == 1 and "not source.revision" in result.stderr, "a tag that is not the revision is refused: " + result.stderr)

        release(change=lambda m: m["source"].update(tag="oscairn-v0.1.5"))
        _, result = verify()
        check(result.returncode == 0, "an oscairn-vX.Y.Z tag verifies: " + result.stdout + result.stderr)
        release(change=lambda m: m["source"].update(tag="oscairn-v0.1.6"))
        _, result = verify()
        check(result.returncode == 1 and "source.tag oscairn-v0.1.6 is not v0.1.5 or oscairn-v0.1.5" in result.stderr,
              "an oscairn-vX.Y.Z tag of another version is refused: " + result.stderr)
        for tag in ("oscairn-0.1.5", "xoscairn-v0.1.5"):
            release(change=lambda m: m["source"].update(tag=tag))
            _, result = verify()
            check(result.returncode == 1 and "source.tag does not match" in result.stderr, f"a {tag} tag is refused: " + result.stderr)
            result = run(sys.executable, "-I", SCRIPT, "verify", "--repo", "oscairn/arpeggiator", "--tag", tag, "--out", temp / "unused")
            check(result.returncode == 1 and "--tag vX.Y.Z or oscairn-vX.Y.Z" in result.stderr, f"--tag {tag} is refused: " + result.stderr)
            result = run(sys.executable, "-I", SCRIPT, "verify", "--release", f"https://github.com/oscairn/arpeggiator/releases/tag/{tag}", "--out", temp / "unused")
            check(result.returncode == 1 and "is not https://github.com/oscairn/<fork>/releases/tag/" in result.stderr, f"a release at {tag} is refused: " + result.stderr)
        # Dependencies on packages of any kind. Another package here: a record is enough for the import's
        # check (build-index.py checks the archive when it is merged).
        def record_of(package_id, release_, needs=(), yanked=None):
            folder = packages / "packages" / package_id
            folder.mkdir(exist_ok=True)
            r = {"id": package_id, "release": release_, "yanked": yanked}
            if needs:
                r["dependencies"] = {"assets": [], "packages": list(needs)}
            (folder / f"{release_}.json").write_text(json.dumps(r))

        def needing(*needs):
            return lambda m: m["dependencies"].update(packages=[{"id": i, "version": v} for i, v in needs])

        record_of("org.oscairn.sample-types", "1.0.0")
        release(change=needing(("org.oscairn.sample-types", "1.0.0")))
        candidate, result = verify()
        plan = json.loads((candidate / "plan.json").read_bytes())
        check(result.returncode == 0 and plan["packageDependencies"] == [{"id": "org.oscairn.sample-types", "version": "1.0.0"}]
              and "--package-dependency org.oscairn.sample-types 1.0.0" in (candidate / "sign.sh").read_text(),
              "a dependency on a package here verifies and is in the plan and sign.sh: " + result.stdout + result.stderr)
        recorded = temp / "packages-recorded"
        shutil.copytree(packages, recorded)
        result = run(sys.executable, "-I", SCRIPT, "sign", "--candidate", candidate, "--key", temp / "keys" / "test.key", "--oscairn-sign", tool)
        check(result.returncode == 0, "it signs: " + result.stdout + result.stderr)
        result = run(sys.executable, "-I", SCRIPT, "record", "--candidate", candidate, "--oscairn-sign", tool, "--packages", recorded)
        r = json.loads((recorded / "packages" / "org.oscairn.arpeggiator" / "0.1.5.json").read_bytes()) if result.returncode == 0 else {}
        check(r.get("dependencies") == {"assets": [], "packages": [{"id": "org.oscairn.sample-types", "version": "1.0.0"}]},
              "the record carries dependencies.packages beside dependencies.assets: " + result.stdout + result.stderr + json.dumps(r.get("dependencies")))
        release(change=needing(("org.oscairn.sample-types", "1.0.0"), ("org.oscairn.sample-types", "0.9")))
        _, result = verify()
        check(result.returncode == 0, "a version already here or earlier verifies: " + result.stderr)
        release(change=needing(("org.oscairn.sample-types", "1.1.0")))
        _, result = verify()
        check(result.returncode == 1 and "needs the package org.oscairn.sample-types 1.1.0 or later, which this repository does not have" in result.stderr,
              "a version later than any here is refused, naming it: " + result.stderr)
        release(change=needing(("org.oscairn.nothing", "1.0.0")))
        _, result = verify()
        check(result.returncode == 1 and "needs the package org.oscairn.nothing 1.0.0 or later" in result.stderr, "a missing package is refused, naming it: " + result.stderr)
        record_of("org.oscairn.yanked", "1.0.0", yanked="broken")
        release(change=needing(("org.oscairn.yanked", "1.0.0")))
        _, result = verify()
        check(result.returncode == 1 and "needs the package org.oscairn.yanked 1.0.0" in result.stderr, "a yanked package is refused: " + result.stderr)
        release(change=needing(("org.oscairn.arpeggiator", "0.1.4")))
        _, result = verify()
        check(result.returncode == 1 and "org.oscairn.arpeggiator cannot depend on itself" in result.stderr, "a package that names itself is refused: " + result.stderr)
        record_of("org.oscairn.sample-types", "1.0.0", needs=[{"id": "org.oscairn.other", "version": "1.0"}])
        record_of("org.oscairn.other", "1.0", needs=[{"id": "org.oscairn.arpeggiator", "version": "0.1.4"}])
        release(change=needing(("org.oscairn.sample-types", "1.0.0")))
        _, result = verify()
        check(result.returncode == 1 and "a cycle of package dependencies: org.oscairn.arpeggiator -> org.oscairn.sample-types -> org.oscairn.other -> org.oscairn.arpeggiator" in result.stderr,
              "a package that makes a cycle is refused: " + result.stderr)
        record_of("org.oscairn.sample-types", "1.0.0")
        shutil.rmtree(packages / "packages" / "org.oscairn.other")
        shutil.rmtree(packages / "packages" / "org.oscairn.yanked")

        for what, change in (("an unknown key in the manifest", lambda m: m.update(extra=1)),
                             ("an unknown key in dependencies", lambda m: m["dependencies"].update(extra=[])),
                             ("an unknown key in a package dependency", lambda m: m["dependencies"].update(packages=[{"id": "org.oscairn.sample-types", "version": "1.0.0", "range": ">1"}])),
                             ("a package dependency without a version", lambda m: m["dependencies"].update(packages=[{"id": "org.oscairn.sample-types"}])),
                             ("an unknown key in init", lambda m: m.update(init={"library": {"file": "a.olib", "sha256": "0" * 64, "size": 1}, "sound": "x", "extra": 1})),
                             ("an unknown key in init.library", lambda m: m.update(init={"library": {"file": "a.olib", "sha256": "0" * 64, "size": 1, "url": "x"}, "sound": "x"})),
                             ("an init without a Sound", lambda m: m.update(init={"library": {"file": "a.olib", "sha256": "0" * 64, "size": 1}}))):
            release(change=change)
            _, result = verify()
            check(result.returncode == 1 and "the manifest is not valid" in result.stderr, f"{what} is refused: " + result.stderr.strip())

        # An init library, published with its plugin under README.md's init library exception: fetched
        # and checked as a build is, kept in the candidate, played by plugin-check, and signed with the
        # plugin by the same key; every condition refused with its message, and nothing written.
        init_id, plays_now = "org.oscairn.arpeggiator.init", (("org.oscairn.arpeggiator", "0.1.4"),)
        source_now = {"url": "https://github.com/oscairn/arpeggiator", "revision": revision}

        def library_of(library_id=init_id, sound="init", plays=plays_now, license_="MIT", source=source_now, extra=None,
                       manifest=lambda m: None, symlink=None, executable=None, second_manifest=None, nodes_list=False, text=True):
            """An init library archive, written entry by entry (as oscairn writes one, library.json first)."""
            document = {"format": "oscairn.sound", "version": 8,
                        "nodes": {f"node-{i + 1}": {"plugin": {"format": "clap", "id": i_, "version": v, "pathHint": ""}} for i, (i_, v) in enumerate(plays)}}
            if nodes_list:
                document["nodes"] = list(document["nodes"].values())
            files = {"Sounds/Init.oscairn/sound.json": json.dumps(document).encode(), **({"Licenses/LICENSE": b"MIT license text\n"} if text else {}), **(extra or {})}
            m = {"format": "oscairn.library", "version": 3, "id": library_id, "name": "Init", "kind": "library", "license": license_, "source": source,
                 "items": [{"kind": "sound", "id": sound, "name": "Init", "path": "Sounds/Init.oscairn", "signatures": []}],
                 "plugins": [], "assets": None, "dependencies": {"assets": []}, "files": {n: hashlib.sha256(d).hexdigest() for n, d in files.items()}}
            manifest(m)
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
                z.writestr("library.json", json.dumps(m))
                if second_manifest:
                    z.writestr("library.json", json.dumps(second_manifest))
                for n, d in files.items():
                    info = zipfile.ZipInfo(n)
                    info.create_system = 3
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.external_attr = (0o100755 if n == executable else 0o120777 if n == symlink else 0o100644) << 16
                    z.writestr(info, d)
            return buffer.getvalue()

        content = library_of()
        record_of(init_id, "0.1.0")  # an earlier init library, by the same publisher
        (packages / "packages" / init_id / "0.1.0.json").write_text(json.dumps({"id": init_id, "release": "0.1.0", "publisher": "org.oscairn", "license": "MIT", "yanked": None}))

        def with_init(data=content, sound="init", sha=None, needs=((init_id, "0.1.5"),), file="init-library.olib", license_=None, size=None):
            def change(m):
                (served / file).write_bytes(data)
                m["init"] = {"library": {"file": file, "sha256": sha or hashlib.sha256(data).hexdigest(), "size": size or len(data)}, "sound": sound}
                if license_:
                    m["init"]["library"]["license"] = license_
                needing(*needs)(m)
            return change

        def tree(root):
            return sorted(str(f.relative_to(root)) for f in root.rglob("*") if f.is_file())

        stub = temp / "stub-plugin-check"
        stub.write_text(f"#!{sys.executable}\nimport json, sys\nopen({str(temp / 'stub-calls')!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\nprint('a note effect: notes out')\n")
        stub.chmod(0o755)
        calls = temp / "stub-calls"

        release(change=with_init())
        candidate, result = verify()
        plan = json.loads((candidate / "plan.json").read_bytes())
        init_plan = plan.get("initPackage", {})
        check(result.returncode == 0 and (candidate / "init" / "init-library.olib").read_bytes() == content
              and plan["init"] == {"library": "init/init-library.olib", "sound": "init"}
              and init_plan.get("id") == init_id and init_plan.get("version") == "0.1.5" and init_plan.get("license") == "MIT"
              and init_plan.get("source") == source_now and init_plan.get("publisher") == "org.oscairn",
              "an init library is fetched into the candidate and planned as <plugin>.init at the plugin's version: " + result.stdout + result.stderr)
        script = (candidate / "sign.sh").read_text()
        check("cp init/init-library.olib out/org.oscairn.arpeggiator.init-0.1.5.olib" in script and f'archive "$key" out/{init_id}-0.1.5.olib' in script
              and "plugin-package" in script and "init-library" not in script.split("plugin-package")[1].split("\n")[0],
              "sign.sh signs the init library with the same key, and it is not in the plugin's package")
        calls.unlink(missing_ok=True)
        result = run(sys.executable, "-I", SCRIPT, "plugin-check", "--candidate", candidate, "--platform", "macos-arm64", "--plugin-check", stub)
        argv = json.loads(calls.read_text().splitlines()[0]) if calls.exists() else []
        check(result.returncode == 0 and argv[-4:] == ["--library", str(candidate.resolve() / "init" / "init-library.olib"), "--sound", "init"],
              "plugin-check passes --library and --sound to oscairn-plugin-check: " + json.dumps(argv) + result.stdout + result.stderr)

        # Both signed, recorded, and the index builds with both in one tree.
        by_hand_init = temp / "by-hand-init"
        shutil.copytree(candidate, by_hand_init)
        result = run(sys.executable, "-I", SCRIPT, "sign", "--candidate", candidate, "--key", temp / "keys" / "test.key", "--oscairn-sign", tool)
        check(result.returncode == 0, "it signs the plugin and its init library: " + result.stdout + result.stderr)
        both = temp / "packages-both"
        shutil.copytree(packages, both)
        result = run(sys.executable, "-I", SCRIPT, "record", "--candidate", candidate, "--oscairn-sign", tool, "--packages", both)
        check(result.returncode == 0 and "the init library, published under the init exception" in result.stdout, "it records both: " + result.stdout + result.stderr)
        made_init = both / "packages" / init_id
        result = run(tool, "verify", made_init / "0.1.5.olib")
        check(result.returncode == 0 and f"archive {init_id} {test_id}" in result.stdout and "build " not in result.stdout,
              "the init library's .olib verifies, signed by the test key, with no build: " + result.stdout + result.stderr)
        r = json.loads((made_init / "0.1.5.json").read_bytes())
        check(r["release"] == "0.1.5" and r["publisher"] == "org.oscairn" and r["license"] == "MIT" and r["source"] == source_now and r["platforms"] == []
              and r["archive"]["path"] == "0.1.5.olib" and r["yanked"] is None and "dependencies" not in r, "its record has the plugin's version, publisher, license and source: " + json.dumps(r))
        r = json.loads((both / "packages" / "org.oscairn.arpeggiator" / "0.1.5.json").read_bytes())
        check(r["dependencies"]["packages"] == [{"id": init_id, "version": "0.1.5"}], "the plugin's record names the init library at this version")
        checked = temp / "checked-both"
        (checked / "packages" / "org.oscairn.arpeggiator").mkdir(parents=True)
        (checked / "packages" / init_id).mkdir(parents=True)
        shutil.copytree(both / "publishers", checked / "publishers")
        shutil.copy(REPO / "repository-config.json", checked)
        shutil.copy(REPO / "revoked.json", checked)
        for f in ("0.1.5.json", "0.1.5.olib"):
            shutil.copy(both / "packages" / "org.oscairn.arpeggiator" / f, checked / "packages" / "org.oscairn.arpeggiator")
            shutil.copy(made_init / f, checked / "packages" / init_id)
        index = lambda root: run(sys.executable, "-I", HERE / "build-index.py", "--check", "--oscairn-sign", tool, "--repo", root, "--cache", temp / "cache-init", "--out", temp / "site-init", "--serial", "0")
        result = index(checked)
        check(result.returncode == 0, "build-index.py --check passes the plugin and its init library arriving together: " + result.stdout + result.stderr)
        shutil.rmtree(checked / "packages" / init_id)
        result = index(checked)
        check(result.returncode != 0 and f"needs the package {init_id} 0.1.5 or later, which this repository does not have" in result.stderr,
              "and refuses the plugin without its init library: " + result.stderr)

        # What was signed is what was checked.
        spec = importlib.util.spec_from_file_location("import_release", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        played = candidate / plan["initPackage"]["library"]
        check(module.derived_problems(played, made_init / "0.1.5.olib") == [], "the published archive has the entries and bytes of the file that was played")
        swapped = library_of(extra={"Licenses/LICENSE": b"another text\n"})
        (temp / "swapped.olib").write_bytes(swapped)
        problems = module.derived_problems(played, temp / "swapped.olib")
        check(any("Licenses/LICENSE differs" in p for p in problems), "a changed entry is found: " + json.dumps(problems))
        (temp / "extra.olib").write_bytes(library_of(extra={"Samples/extra.osp": b"x"}))
        check(any("Samples/extra.osp is only in" in p for p in module.derived_problems(played, temp / "extra.olib")), "an added entry is found")
        # record refuses an archive signed from other content than the file played.
        tampered = temp / "candidate-tampered"
        shutil.copytree(by_hand_init, tampered)
        result = run(sys.executable, "-I", SCRIPT, "sign", "--candidate", tampered, "--key", temp / "keys" / "test.key", "--oscairn-sign", tool)
        shutil.copyfile(temp / "swapped.olib", tampered / plan["initPackage"]["archive"])
        run(tool, "archive", temp / "keys" / "test.key", tampered / plan["initPackage"]["archive"])
        fresh = temp / "packages-tampered"
        shutil.copytree(packages, fresh)
        before = tree(fresh)
        result = run(sys.executable, "-I", SCRIPT, "record", "--candidate", tampered, "--oscairn-sign", tool, "--packages", fresh)
        check(result.returncode == 1 and "is not the file that was checked: Licenses/LICENSE differs" in result.stderr and tree(fresh) == before,
              "record refuses an init archive made from other content, writing nothing: " + result.stderr)
        # sign refuses a file changed after verify.
        changed = temp / "candidate-changed"
        shutil.copytree(by_hand_init, changed)
        (changed / plan["initPackage"]["library"]).write_bytes(swapped)
        result = run(sys.executable, "-I", SCRIPT, "sign", "--candidate", changed, "--key", temp / "keys" / "test.key", "--oscairn-sign", tool)
        check(result.returncode == 1 and "is not the SHA-256 verify checked" in result.stderr and not (changed / "out").exists(), "sign refuses an init file that is not the one verify checked: " + result.stderr)
        # And by hand, sign.sh gives the same.
        result = run("sh", by_hand_init / "sign.sh", Path("keys") / "test.key", Path(tool).resolve(), cwd=temp)
        check(result.returncode == 0 and module.derived_problems(played, by_hand_init / plan["initPackage"]["archive"]) == [], "sign.sh signs the init library too: " + result.stdout + result.stderr)

        # The maintainers' writer: nodes a list, and no assets, dependencies or item signatures (but a license and source).
        def writer_made(m):
            for field in ("assets", "dependencies"):
                del m[field]
            del m["items"][0]["signatures"]
        data = library_of(manifest=writer_made, nodes_list=True)
        release(change=with_init(data=data))
        candidate, result = verify()
        check(result.returncode == 0, "an init library as the maintainers' writer makes it (nodes a list, no assets, dependencies or item signatures) verifies: " + result.stderr)
        result = run(sys.executable, "-I", SCRIPT, "sign", "--candidate", candidate, "--key", temp / "keys" / "test.key", "--oscairn-sign", tool)
        recorded_writer = temp / "packages-writer"
        shutil.copytree(packages, recorded_writer)
        check(result.returncode == 0, "and signs: " + result.stdout + result.stderr)
        result = run(sys.executable, "-I", SCRIPT, "record", "--candidate", candidate, "--oscairn-sign", tool, "--packages", recorded_writer)
        r = json.loads((recorded_writer / "packages" / init_id / "0.1.5.json").read_bytes()) if result.returncode == 0 else {}
        check(result.returncode == 0 and r.get("license") == "MIT" and r.get("source") == source_now, "and records, with the license and the source: " + result.stderr + json.dumps(r))

        release()
        candidate, result = verify()
        calls.unlink(missing_ok=True)
        result = run(sys.executable, "-I", SCRIPT, "plugin-check", "--candidate", candidate, "--platform", "macos-arm64", "--plugin-check", stub)
        argv = json.loads(calls.read_text().splitlines()[0]) if calls.exists() else []
        script = (candidate / "sign.sh").read_text()
        check(result.returncode == 0 and "--library" not in argv and "--sound" not in argv and "initPackage" not in json.loads((candidate / "plan.json").read_bytes())
              and "init" not in script.replace("# ", ""), "a release without init is unchanged: neither is passed, nothing is planned for it: " + json.dumps(argv))

        # A version removed.json lists is never imported again.
        release()
        (packages / "removed.json").write_text(json.dumps({"format": "oscairn.repository-removals", "version": 1, "versions": [
            {"id": "org.oscairn.arpeggiator", "version": "0.1.5", "reason": "test", "date": "2026-11-17"}]}))
        _, result = verify()
        check(result.returncode == 1 and "org.oscairn.arpeggiator 0.1.5 was removed (removed.json)" in result.stderr, "a removed id and version is refused: " + result.stderr.strip()[-300:])
        (packages / "removed.json").unlink()
        release()

        # A first release of a new package id: only when asked for, and only for an id with no release here.
        def new_id(m):
            m["package"]["id"] = "org.oscairn.brandnew"
        release(change=new_id)
        before = tree(packages)
        candidate, result = verify()
        check(result.returncode == 1 and "no release of org.oscairn.brandnew here yet" in result.stderr and "--first-release" in result.stderr
              and not (candidate / "plan.json").exists() and tree(packages) == before, "a new id without the flag is refused, naming it: " + result.stderr.strip()[-300:])
        candidate, result = verify("--first-release")
        first_plan = json.loads((candidate / "plan.json").read_bytes()) if result.returncode == 0 else {}
        shown = (candidate / "first-release.txt").read_text() if (candidate / "first-release.txt").exists() else ""
        check(result.returncode == 0 and first_plan.get("firstRelease") is True and first_plan.get("publisher") == "org.oscairn" and first_plan.get("tags") == []
              and "license: MIT" in shown and "source: https://github.com/oscairn/arpeggiator v0.1.5 (" + revision + ")" in shown and "license: MIT" in result.stdout
              and tree(packages) == before, "a new id with the flag is accepted, and its license and source are shown: " + result.stdout + result.stderr)
        result = run(sys.executable, "-I", SCRIPT, "sign", "--candidate", candidate, "--key", temp / "keys" / "test.key", "--oscairn-sign", tool)
        first_packages = temp / "packages-first"
        shutil.copytree(packages, first_packages)
        result = run(sys.executable, "-I", SCRIPT, "record", "--candidate", candidate, "--oscairn-sign", tool, "--packages", first_packages)
        r = json.loads((first_packages / "packages" / "org.oscairn.brandnew" / "0.1.5.json").read_bytes()) if result.returncode == 0 else {}
        check(result.returncode == 0 and r.get("publisher") == "org.oscairn" and r.get("license") == "MIT", "and it signs and records, for Oscairn's publisher: " + result.stdout + result.stderr)
        release()
        _, result = verify("--first-release")
        check(result.returncode == 1 and "org.oscairn.arpeggiator has a release here already" in result.stderr, "the flag on an id that has a release is refused: " + result.stderr.strip()[-300:])
        (packages / "removed.json").write_text(json.dumps({"format": "oscairn.repository-removals", "version": 1, "versions": [
            {"id": "org.oscairn.brandnew", "version": "0.9.0", "reason": "test", "date": "2026-11-17"}]}))
        release(change=new_id)
        for extra in ((), ("--first-release",)):
            _, result = verify(*extra)
            check(result.returncode == 1 and "removed.json lists it" in result.stderr, f"a removed id is refused {'with' if extra else 'without'} the flag: " + result.stderr.strip()[-300:])
        (packages / "removed.json").unlink()
        release()

        # Refusals: each condition, with its message, and nothing written by verify.
        def refused(case, wanted, **options):
            before = tree(packages)
            change = options.pop("change", None)
            release(change=change or with_init(**options))
            candidate, result = verify()
            check(result.returncode == 1 and wanted in result.stderr and not (candidate / "plan.json").exists() and not (candidate / "sign.sh").exists() and tree(packages) == before,
                  f"{case} is refused, with nothing planned or written: " + result.stderr.strip()[-400:])

        refused("a tampered init library", "init library) is not the size and SHA-256 the manifest gives", sha="1" * 64)
        refused("an init library that is not an archive", "is not a library archive", data=b"not an archive")
        refused("an init library its package does not need", "dependencies.packages must name org.oscairn.arpeggiator.init at 0.1.5", needs=())
        refused("an init file named as the manifest", "is also another file of the release", file="oscairn-release.json")
        # (a) the id
        record_of("org.oscairn.sample-content", "1.0.0")
        refused("another library's id (not <plugin>.init)", "a release may publish only its own init library, org.oscairn.arpeggiator.init",
                data=library_of(library_id="org.oscairn.sample-content"), needs=(("org.oscairn.sample-content", "1.0.0"),))
        refused("a library under the plugin's id but one more part", "a release may publish only its own init library", data=library_of(library_id=init_id + ".extra"))
        refused("an init library under another plugin's id", "a release may publish only its own init library", data=library_of(library_id="org.oscairn.sampler.init"))
        # (b) a brand-new plugin
        refused("an init library with a brand-new plugin (no --first-release)", "no release of org.oscairn.brandnew here yet: a first release of a new package id is imported only when asked for (--first-release",
                change=lambda m: (with_init(data=library_of(library_id="org.oscairn.brandnew.init"), needs=(("org.oscairn.brandnew.init", "0.1.5"),))(m),
                                  m["package"].update(id="org.oscairn.brandnew")))
        # (c) the publisher
        (packages / "packages" / init_id / "0.1.0.json").write_text(json.dumps({"id": init_id, "release": "0.1.0", "publisher": "org.other", "license": "MIT", "yanked": None}))
        refused("an init library whose earlier release is another publisher's", "is published by org.other, not org.oscairn")
        (packages / "packages" / init_id / "0.1.0.json").write_text(json.dumps({"id": init_id, "release": "0.1.0", "publisher": "org.oscairn", "license": "MIT", "yanked": None}))
        # (d) the license and source
        refused("an init library under another license than the plugin's", "library.json's license is \"GPL-3.0-or-later\", not MIT", data=library_of(license_="GPL-3.0-or-later"))
        refused("an init library whose stated license is not the archive's", "library.json's license is \"MIT\", not CC0-1.0", license_="CC0-1.0")
        release(change=with_init(data=library_of(license_="CC0-1.0"), license_="CC0-1.0"))
        candidate, result = verify()
        check(result.returncode == 1 and "the init library's license is not" in result.stderr and "MIT becomes CC0-1.0" in result.stderr,
              "a license other than the init library's last release's is refused: " + result.stderr)
        candidate, result = verify("--accept-license-change")
        check(result.returncode == 0 and json.loads((candidate / "plan.json").read_bytes())["initPackage"]["license"] == "CC0-1.0",
              "init.library.license is the record's license once the change is accepted: " + result.stdout + result.stderr)
        refused("an init library with no license", "library.json's license is null, not MIT", data=library_of(manifest=lambda m: m.pop("license")))
        refused("an init library with no source", "library.json's source is null", data=library_of(manifest=lambda m: m.pop("source")))
        refused("an init library with a LicenseRef- license and no text under Licenses/", "names a LicenseRef- and the archive has no text under Licenses/",
                data=library_of(license_="LicenseRef-X", text=False), license_="LicenseRef-X")
        refused("an init library whose source is not the plugin's", "library.json's source is", data=library_of(source={"url": "https://github.com/oscairn/arpeggiator", "revision": "2" * 40}))
        # (e) the contents
        refused("an entry that climbs out", "climbs out", data=library_of(extra={"Samples/../../evil": b"x"}))
        refused("an absolute entry", "is absolute", data=library_of(extra={"/etc/evil": b"x"}))
        refused("an entry with a backslash", "uses a backslash", data=library_of(extra={"Samples\\evil": b"x"}))
        refused("a hidden entry", "is hidden", data=library_of(extra={"Licenses/.hidden": b"x"}))
        refused("a symbolic link", "is a symbolic link", data=library_of(extra={"Licenses/link": b"/etc/passwd"}, symlink="Licenses/link"))
        refused("an executable file", "nothing executable goes in an init library", data=library_of(extra={"Licenses/run": b"text"}, executable="Licenses/run"))
        refused("an executable's bytes", "is an executable file", data=library_of(extra={"Samples/x.osp": b"\x7fELF" + bytes(60)}))
        refused("a plugin build inside", "is outside Sounds/, Samples/", data=library_of(extra={"Plugins/clap/x@1/linux-arm64/x.clap": b"x"}))
        refused("a manifest listing plugins", "lists plugins or an asset library", data=library_of(manifest=lambda m: m.update(plugins=[{"format": "clap", "id": "x", "version": "1", "name": "x", "builds": []}])))
        refused("assets outside the folders", "is outside Sounds/", data=library_of(extra={"Assets/abc/big.wav": b"x"}))
        refused("a second library.json (a second library id)", "is not a library archive: it has an entry twice",
                data=library_of(second_manifest={"format": "oscairn.library", "version": 3, "id": "org.evil.other", "name": "x", "kind": "library", "items": [], "plugins": [], "files": {}}))
        refused("entries that differ only in case", "differ only in case", data=library_of(extra={"Licenses/A": b"1", "Licenses/a": b"2"}))
        refused("more than 8 MiB of content", "over the 8388608 an init library may hold", data=library_of(extra={"Samples/big.osp": bytes(9 << 20)}))
        refused("a declared size over 8 MiB", "init.library.size 9000000 is over the 8388608 bytes", size=9000000)
        refused("a file not as library.json lists it", "is not the SHA-256 library.json lists", data=library_of(manifest=lambda m: m["files"].update({"Licenses/LICENSE": "0" * 64})))
        refused("a Sound that is not the one init.sound names", "library.json's items are not exactly the one Sound other", sound="other")
        refused("a Sound saved with another version of the plugin", "was saved with org.oscairn.arpeggiator 0.1.3, and this release's is 0.1.4", data=library_of(plays=(("org.oscairn.arpeggiator", "0.1.3"),)))
        refused("a Sound playing another plugin", "plays org.oscairn.other, which this release does not have", data=library_of(plays=(("org.oscairn.arpeggiator", "0.1.4"), ("org.oscairn.other", "1"))))
        refused("a Sound with no plugin", "plays none of this release's plugins", data=library_of(plays=()))
        # (f) a version never changes
        (packages / "packages" / init_id / "0.1.5.json").write_text(json.dumps({"id": init_id, "release": "0.1.5", "publisher": "org.oscairn", "license": "MIT", "yanked": None}))
        refused("an init library version already here", f"{init_id} 0.1.5 is here already: a version never changes once merged")
        (packages / "packages" / init_id / "0.1.5.json").unlink()
        # (g) the plugin names it at its own version, and the init exception reaches no other dependency
        refused("a dependency on an older init library", "dependencies.packages must name org.oscairn.arpeggiator.init at 0.1.5", needs=((init_id, "0.1.4"),))
        release(change=with_init(needs=((init_id, "0.1.5"), ("org.oscairn.nothing", "1.0.0"))))
        _, result = verify()
        check(result.returncode == 1 and "needs the package org.oscairn.nothing 1.0.0 or later" in result.stderr, "the exception covers the init library only: another missing package is still refused: " + result.stderr)
        shutil.rmtree(packages / "packages" / init_id)
        server.shutdown()
    finally:
        shutil.rmtree(temp, ignore_errors=True)
    print("FAILED" if failures else "PASSED")
    return 1 if failures else 0


if __name__ == "__main__":
    if len(sys.argv) not in (2, 3):
        sys.exit(__doc__)
    sys.exit(main(*sys.argv[1:]))
