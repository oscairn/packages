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
package's version. The key lives only in a temporary folder.
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
        server.shutdown()
    finally:
        shutil.rmtree(temp, ignore_errors=True)
    print("FAILED" if failures else "PASSED")
    return 1 if failures else 0


if __name__ == "__main__":
    if len(sys.argv) not in (2, 3):
        sys.exit(__doc__)
    sys.exit(main(*sys.argv[1:]))
