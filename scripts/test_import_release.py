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
through a stand-in for oscairn-plugin-check) are covered too, as are unknown keys. The key lives only
in a temporary folder.
"""
import functools
import hashlib
import http.server
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
        shutil.copytree(PREVIOUS, packages / "packages" / PREVIOUS.name, ignore=shutil.ignore_patterns("0.1.3*"))
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

        for url, listed in (("https://github.com/oscairn/other", False), ("https://github.com/oscairn/va", True), ("https://github.com/oscairn/sampler", True)):
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

        # An init library: fetched and checked as a build, kept in the candidate, passed to the check on
        # every platform, and in no package.
        def library_of(package_id):
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as z:
                z.writestr("library.json", json.dumps({"format": "oscairn.library", "version": 3, "id": package_id}))
            return buffer.getvalue()

        content = library_of("org.oscairn.sample-content")
        record_of("org.oscairn.sample-content", "1.0.0")

        def with_init(data=content, sound="org.oscairn.sample-content.piano", sha=None, needs=(("org.oscairn.sample-content", "1.0.0"),), file="init-library.olib"):
            def change(m):
                (served / file).write_bytes(data)
                m["init"] = {"library": {"file": file, "sha256": sha or hashlib.sha256(data).hexdigest(), "size": len(data)}, "sound": sound}
                needing(*needs)(m)
            return change

        stub = temp / "stub-plugin-check"
        stub.write_text(f"#!{sys.executable}\nimport json, sys\nopen({str(temp / 'stub-calls')!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\nprint('a note effect: notes out')\n")
        stub.chmod(0o755)
        calls = temp / "stub-calls"

        release(change=with_init())
        candidate, result = verify()
        plan = json.loads((candidate / "plan.json").read_bytes())
        check(result.returncode == 0 and (candidate / "init" / "init-library.olib").read_bytes() == content
              and plan["init"] == {"library": "init/init-library.olib", "sound": "org.oscairn.sample-content.piano"}
              and "init-library" not in (candidate / "sign.sh").read_text() and "piano" not in (candidate / "sign.sh").read_text(),
              "an init library is fetched into the candidate and kept out of sign.sh: " + result.stdout + result.stderr)
        calls.unlink(missing_ok=True)
        result = run(sys.executable, "-I", SCRIPT, "plugin-check", "--candidate", candidate, "--platform", "macos-arm64", "--plugin-check", stub)
        argv = json.loads(calls.read_text().splitlines()[0]) if calls.exists() else []
        check(result.returncode == 0 and argv[-4:] == ["--library", str(candidate.resolve() / "init" / "init-library.olib"), "--sound", "org.oscairn.sample-content.piano"],
              "plugin-check passes --library and --sound to oscairn-plugin-check: " + json.dumps(argv) + result.stdout + result.stderr)
        release()
        candidate, result = verify()
        calls.unlink(missing_ok=True)
        result = run(sys.executable, "-I", SCRIPT, "plugin-check", "--candidate", candidate, "--platform", "macos-arm64", "--plugin-check", stub)
        argv = json.loads(calls.read_text().splitlines()[0]) if calls.exists() else []
        check(result.returncode == 0 and "--library" not in argv and "--sound" not in argv and "init" not in json.loads((candidate / "plan.json").read_bytes()),
              "without init, neither is passed: " + json.dumps(argv))
        release(change=with_init(sha="1" * 64))
        _, result = verify()
        check(result.returncode == 1 and "init library) is not the size and SHA-256 the manifest gives" in result.stderr, "a tampered init library is refused: " + result.stderr)
        release(change=with_init(data=b"not an archive"))
        _, result = verify()
        check(result.returncode == 1 and "is not a library archive" in result.stderr, "an init library that is not an archive is refused: " + result.stderr)
        release(change=with_init(needs=(("org.oscairn.sample-types", "1.0.0"),)))
        _, result = verify()
        check(result.returncode == 1 and "the init library is org.oscairn.sample-content, which the manifest's dependencies.packages does not name" in result.stderr,
              "an init library its package does not need is refused: " + result.stderr)
        release(change=with_init(file="oscairn-release.json"))
        _, result = verify()
        check(result.returncode == 1 and "is also another file of the release" in result.stderr, "an init file named as the manifest is refused: " + result.stderr)
        server.shutdown()
    finally:
        shutil.rmtree(temp, ignore_errors=True)
    print("FAILED" if failures else "PASSED")
    return 1 if failures else 0


if __name__ == "__main__":
    if len(sys.argv) not in (2, 3):
        sys.exit(__doc__)
    sys.exit(main(*sys.argv[1:]))
