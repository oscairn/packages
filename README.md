# Oscairn packages

Submissions to Oscairn's official package repository
([RFC 0031](https://github.com/oscairn/oscairn/blob/main/docs/rfcs/0031-package-repository-and-distribution.md)).
A pull request is a submission. GitHub Actions checks it, and on merge builds and signs the index,
puts package files in this repository's releases and the signed index on GitHub Pages. There is
no server.

## Downloading a package by hand

Each version of each package has a ready-made archive per platform, to download in a browser and
install from Oscairn's Library (Install..., File), or to carry to a device with no network:

```
https://packages.oscairn.org/exports/<package ID>/<version>/<package ID>-<version>-<platform>.olib
```

such as `https://packages.oscairn.org/exports/com.example.organ/2.1.0/com.example.organ-2.1.0-macos-arm64.olib`,
the platform one of `linux-arm64`, `linux-x86_64`, `macos-arm64`, `macos-x86_64` and
`windows-x86_64`. A package with no plugin builds (a library of Sounds, an asset library) has one,
`<package ID>-<version>.olib`. That address is a page that sends the browser on to the file, a
release asset of this repository (`exports-<package ID>`); link to the address, not to the asset,
so the link survives a move of the hosting. It holds the release's own manifest and signature,
what every platform shares and that platform's builds, and installs with every check an archive
gets. There is none for a version over 1,900 MiB, a withdrawn one, or a platform whose build was
revoked; the Catalog offers only those that exist. Devices never need them: they fetch each
release in pieces.

> **Status (2026-11-16).** Dependencies on packages of any kind (`dependencies.packages`), the rule
> that a sample or waveform file is a piece of its own, and the manifest's `init` field are in this
> repository. The first release of the types package, `org.oscairn.sample-types` 1.0.0, is not:
> a new package's first release is signed by the owner by hand (`import-release.py` refuses an id
> with no earlier release; the one exception, a plugin's own `<plugin>.init` library, is described
> under "The init library exception"), and no record of it has been added.

## Submitting a package

You need `oscairn-sign`, built with Oscairn (`cmake --build build --target oscairn-sign`).

1. **Make a key**, once, and keep it safe. It never leaves your machine:

   ```sh
   oscairn-sign key ~/oscairn/publisher.key
   ```

   It prints your key's ID (its fingerprint) and public key.

2. **Sign each plugin build**, then the whole archive. A repository requires both:

   ```sh
   oscairn-sign build ~/oscairn/publisher.key clap com.example.organ 2.1.0 linux-arm64 Organ.clap
   # a plugin alone: pack the signed builds into a plugin package, signed whole
   oscairn-sign plugin-package --key ~/oscairn/publisher.key --license GPL-3.0-or-later --license-file COPYING \
       --source https://github.com/example/organ v2.1.0 \
       com.example.organ-2.1.0.olib clap com.example.organ 2.1.0 "Example Organ" linux-arm64=Organ.clap
   # or export the library archive (it carries Organ.clap.sig), then:
   oscairn-sign archive ~/oscairn/publisher.key com.example.organ-2.1.0.olib
   oscairn-sign verify com.example.organ-2.1.0.olib
   ```

   `plugin-package` refuses a build that loads libraries from outside the system and its own
   bundle, such as Homebrew's in `/opt/homebrew`: a device does not have them, and the checks
   refuse such a build too. Bundle them or link them statically.

   A plugin package names its license (`--license`, an SPDX expression) and carries its license
   texts and notices (`--license-file`, each into `Licenses/`). Under the GPL, LGPL or AGPL it
   must also name its source, the exact commit or tag (`--source <url> <revision>`), or it is
   refused: that is the license's "directions next to the object code".

   An asset library (samples, patches, wavetables a plugin reads) is its own package, packed
   from a folder; a plugin package that needs it names it with `--asset-dependency`:

   ```sh
   oscairn-sign asset-package --key ~/oscairn/publisher.key --license GPL-3.0-or-later --license-file NOTICE \
       com.example.factory-1.0.olib com.example.factory 1.0 "Example factory content" factory/
   ```

   A package of any kind may need other packages (a plugin package its types package, a sample
   package the sample types, a Performance pack a sample package): name each with
   `--package-dependency <id> <least version>` on `plugin-package` or `library-package`. A device
   installs them depth first, from the repository the install is from, each at that version or
   later. The record lists them in `dependencies.packages` ([below](#packages)).

   A bundle holding several plugins is signed for each of them at once and packed once, each
   plugin named with `--plugin` (a preset-discovery provider in it is not listed or signed):

   ```sh
   oscairn-sign build ~/oscairn/publisher.key clap com.example.organ,com.example.leslie 2.1.0 linux-arm64 Organ.clap
   oscairn-sign plugin-package --key ~/oscairn/publisher.key --license GPL-3.0-or-later --license-file COPYING \
       --source https://github.com/example/organ v2.1.0 --plugin com.example.organ "Example Organ" \
       --plugin com.example.leslie "Example Leslie" com.example.organ-2.1.0.olib clap com.example.organ 2.1.0 "Example Organ" linux-arm64=Organ.clap
   ```

3. **Open a pull request** adding:

   - on your first submission, `publishers/<your publisher ID>.json` ([below](#publishers)), and
     `https://<your domain>/.well-known/oscairn-publisher.json` naming your public key;
   - `packages/<package ID>/<version>.json` ([below](#packages)), with a link to the archive
     (your own release page, for example), or the archive itself beside it as
     `packages/<package ID>/<version>.olib` if it is under 100 MiB, which is the largest file
     GitHub accepts in a repository.

   The archive is the whole release, signed once: every platform's builds in one archive. On
   merge, publishing splits it into pieces (its manifest, each item, large samples, and each
   platform's builds; RFC 0031, "Pieces: the format"). A file under `Assets/` of 1 MiB or more
   is a piece of its own; so is every file directly in the folder of a type the archive declares
   with a `handler` (`Samples/<id>.osp`, `Waveforms/<id>.owf`), however small, so packages and
   versions share it by blob, so a device downloads what all platforms
   share and its own platform's builds only, and assembles from the pieces the
   [ready-made archives](#downloading-a-package-by-hand). Nothing is signed again: an archive
   made for one device carries your manifest and signature unchanged. Such an archive (one that
   leaves out builds its manifest lists) is not a submission.

The checks report on the pull request. A maintainer reviews a publisher's first submission and
any package that newly brings plugin or app code; other submissions are merged once the checks
pass.

**A version never changes once merged.** Publish a fix as a new version. To yank one, set its
`yanked` to a reason: it is no longer offered, but Performances that use it still get it.

**Removing a version is a maintainer's decision.** `removed.json` lists versions deleted on
purpose (`{"format": "oscairn.repository-removals", "version": 1, "versions": [{"id", "version",
"reason", "date"}]}`, each field required, the date `YYYY-MM-DD`). The check lets exactly those
releases disappear, still refuses any other change or disappearance, and refuses a listed version
whose record is back; the import never publishes a listed id and version again. Use it only when
an ID changes hands; a withdrawn version (`revoked.json`) stays listed, a removed one does not.
Devices keep what they installed, and the version is no longer offered.

## The layout

### Publishers

`publishers/com.example.json`, one per publisher, named by its ID:

```json
{
  "format": "oscairn.repository-publisher",
  "version": 1,
  "id": "com.example",
  "name": "Example Audio",
  "key": "<your public key, 64 hex digits>",
  "verification": "https://example.com/.well-known/oscairn-publisher.json"
}
```

A publisher's key cannot be changed by a pull request yet.

### Packages

`packages/com.example.organ/2.1.0.json`, one per version. The package ID is under the
publisher's ID:

```json
{
  "format": "oscairn.repository-release",
  "version": 1,
  "id": "com.example.organ",
  "release": "2.1.0",
  "publisher": "com.example",
  "summary": "A tonewheel organ and rotary speaker",
  "license": "GPL-3.0-or-later",
  "tags": ["organ", "keys"],
  "channel": "stable",
  "platforms": ["linux-arm64", "macos-arm64"],
  "requires": { "oscairn": "1.4" },
  "source": { "url": "https://github.com/example/organ", "revision": "v2.1.0" },
  "archive": { "url": "https://example.com/releases/organ-2.1.0.olib", "sha256": "<64 hex digits>", "size": 21534720 },
  "yanked": null
}
```

- `source`: the archive's own `source`, exactly (`url` and the commit or tag as `revision`);
  leave it out, or `null`, only when the archive names none. A GPL, LGPL or AGPL plugin
  package always names one.
- `dependencies`: `{"assets": [{"id", "version"}]}`, the asset libraries the archive needs
  (`oscairn-sign plugin-package --asset-dependency <id> <least version>`), exactly as the
  archive names them; leave it out when it needs none. Each must be an asset package in this
  repository, or in the same pull request, at that version or later.
- `dependencies.packages`: `[{"id", "version"}]`, the packages of any kind the archive needs
  (`--package-dependency <id> <least version>`), exactly as the archive names them, beside
  `assets` (`{"assets": [], "packages": [...]}`); leave `packages` out when there are none. Each
  must be a package in this repository, not yanked, at that version or later; the check refuses
  the release otherwise (`needs the package <id> <version> or later, which this repository does
  not have`). The index lists a version's `requires.packages` as its asset libraries followed by
  its packages.
- `archive`: an `https://` `url`, or a `path` naming the archive beside this file; its SHA-256
  and size in bytes. Under 1,900 MiB, since release assets must be under 2 GiB.
- `platforms`: each platform the package has plugin builds for; every plugin must have a build
  for each. Leave it out for a package with no plugins.
- `channel`: `stable` (the default) or `beta`.
- `license`: an SPDX expression; the archive's own when it names one (above).

### Revocations

`revoked.json` lists revoked publisher keys, plugin build digests and withdrawn package versions,
each with a `reason` and a `date`. Maintainers change it, following the takedown policy (below).
The format, as devices read it ([RFC 0031](https://github.com/oscairn/oscairn/blob/main/docs/rfcs/0031-package-repository-and-distribution.md#the-revocation-list)):

```json
{
  "format": "oscairn.repository-revocations",
  "version": 1,
  "keys": [{"key": "<64 hex digits>", "reason": "Its private key was published", "date": "2026-11-11"}],
  "versions": [{"id": "com.example.organ", "version": "2.1.0", "reason": "Its build deletes user files", "date": "2026-11-11"}],
  "builds": [{"sha256": "<64 hex digits>", "reason": "Malware found by a scanner", "date": "2026-11-11"}]
}
```

- `keys`: publisher keys revoked, each a whole public key (not its 16-digit ID). Every package
  the key signed is withdrawn on devices: from this repository's listing, and from every
  repository's when this is the official one. An old index key goes here too when it is
  replaced; a device refuses a root whose own list revokes its root key or the index key that
  signed it.
- `versions`: package versions withdrawn (`id` and `version`), in this repository only.
- `builds`: plugin builds revoked by their digest, as the archive's manifest lists it. Only its
  own platform is affected: a device on that platform never installs it (from any repository,
  when this is the official one), that platform's ready-made archive is no longer made, and the
  other platforms keep the version.
- Every entry has a non-empty `reason`, shown to users, and the `date` it was decided
  (`YYYY-MM-DD`). All three lists are present, empty when there is nothing in them.

**Withdrawn is not yanked.** A yanked version is the publisher's advice, set in its release
record: it is not offered, but a Performance whose lock names it still gets it. A withdrawn
version (by `versions`, a revoked key or a revoked build) is the maintainers' decision: it is
never offered or installed, even when named.

**What devices do.** A device reads the list with the signed root on every refresh. The Catalog
shows a withdrawn version as "withdrawn by <repository>: <reason>" and never offers it. A copy
already installed (its version withdrawn, or signed by a revoked key) is reported to its user
with the reason, with Update when a newer version exists and how to remove it; it is never
deleted or disabled, and nothing interrupts. Devices do not record build digests of what is
installed, so a revoked build already installed is not reported. An archive installed from a file
(a ready-made archive, say) is checked against the list each repository a device has on gave it
last, with no network. A withdrawn version's ready-made archives, and a revoked build's
platform's, are removed from the releases at the next publish.

**Adding an entry.** Open a pull request that adds the entry to `revoked.json` with its reason
and today's date; `CODEOWNERS` requires a maintainer's review. The check runs
`scripts/build-index.py`, which refuses a list a device could not read, and more: a list
missing, an entry without its fields or with an unknown one, a key or digest not 64 lowercase
hex digits, a package ID or version not shaped as a release's, a date that is not a real
`YYYY-MM-DD` day, an empty reason, a repeated entry or member, text over 4096 bytes, and an
entry revoking this repository's own root key or current index key. Each problem names the entry
(`revoked.json: keys[0]: ...`). A withdrawn version that names no release here is a warning, not
a refusal. When a key in `publishers/` is revoked, the releases it signed that are already
published stay listed, so devices can say why they are withdrawn, and a new release signed by it
is refused. On merge, publish puts the list in the signed root.

The index builder publishes the file as `revocations/<sha256>.json`, named in the signed root by
its path, SHA-256 and size, so the index key's signature over the root covers it and it comes
with the root's serial: no older list can be served without its older root, which devices
refuse. A root without one, or with one a device cannot read, makes that repository unusable on
devices until fixed.

**Takedown policy.**

> **TODO (owner): not written yet.** Who decides a takedown, how to report a package (for
> safety, or for rights), and how a publisher appeals. Until it is written, no takedown process is
> published.

## For maintainers

### What the checks do

`.github/workflows/check.yml` builds `oscairn-sign` at a pinned commit of Oscairn and runs
`scripts/build-index.py --check` from the base branch: every record's schema, each archive's
hash, archive safety and every signature through `oscairn-sign verify`, the archive and every
build signed by the publisher's key, each claimed platform's build, that no build loads a
library from outside the system and its bundle (a Homebrew library, say), that the record's
`license`, `source` and `dependencies` are the archive's, that each asset library a release
needs is here at that version or later, that each package a release names in
`dependencies.packages` is here, of any kind and not yanked, at that version or later, that a package keeps the
publisher of its other releases and that publisher IDs do not nest, and no published version or
publisher key changed. It splits each release into pieces and assembles each ready-made archive
from them, checking each with `oscairn-sign verify --platform`, as publishing does; publish then
uploads new pieces to the `blobs-<ab>` releases and the archives to `exports-<package ID>`, and
removes archives no longer made. Each check RFC 0031 lists that is not built yet is its own step that says
so with a warning. Changes outside `publishers/`, `packages/` and `revoked.json` need a
maintainer's review: a pull request can change the workflows it runs under, so `CODEOWNERS`
requires a maintainer for `.github/`, `scripts/`, `revoked.json`, `repository-config.json` and
any `.gitattributes`, and publish re-checks that no published release or publisher key changed
since the commit tagged `published`, whatever the pull request's own check said. It reads that
commit's records as raw blobs, so no `.gitattributes` can hide or rewrite them, and fails when
the tag is missing once any release or publisher is recorded: if the tag is lost, put it back on
the last published commit. `CODEOWNERS` holds only while `main`'s branch protection requires
review from Code Owners.

### Importing a release

Oscairn's own plugins (its forks of Dexed, Surge, sfizz, sst-effects and setBfree, the
Arpeggiator, and its first-party VA, Sampler and FM) are released by tagging `vX.Y.Z` or `oscairn-vX.Y.Z` on the fork, whose CI publishes
a GitHub release: one build file per platform, `licenses.tar.gz` and `oscairn-release.json`, the manifest
([`schemas/plugin-release.schema.json`](schemas/plugin-release.schema.json)). Each plugin in it
has a `kind`, `synth`, `effect` or `note-effect`, which says how `oscairn-plugin-check` plays it:
neither the manifest's other fields nor a release record says so per plugin (a record's `tags`
are the package's).

`.github/workflows/import.yml`, run by hand (Actions, Import a release) with the release's
address or its fork and tag, imports one through `scripts/import-release.py`:

1. **verify** fetches the manifest and its files and checks the manifest against the schema, every
   file's SHA-256 and size, that the source is one of Oscairn's plugin repositories and its tag is
   `source.revision` there, each build's libraries, and that the license and license files are
   the package's last release's (a difference fails, unless the run accepts a license change).
   The packages in the manifest's `dependencies.packages` (the schema allows only
   `{id, version}` beside `dependencies.assets`) must be here, not yanked, at that version or
   later, must not be the package itself, and must not need it back, directly or through others;
   the import refuses otherwise, naming the package, and `record` writes them into the record's
   `dependencies.packages` and `sign.sh` into `--package-dependency` options (the plugin's own
   init library, which arrives in the same run, is the one exception to "must be here").
   An instrument that is silent until it has content names an init library:
   `"init": {"library": {"file", "sha256", "size", "license"?}, "sound": "<Sound item ID>"}`, an archive of a
   library shipped as another file of the release, and the Sound in it. verify fetches it as it
   does a build (SHA-256, size, an archive whose `library.json` has an ID) and requires that ID
   in `dependencies.packages`; it stays in the candidate and is **not** in the plugin's package:
   the library is a package of its own. plugin-check passes `--library <file> --sound <Sound>` to
   `oscairn-plugin-check` for each plugin on every platform. The init library is published with
   the plugin, under the init library exception, below.
   Libraries are read from the files here: a macOS build only `/usr/lib/` and `/System/` and no
   `LC_RPATH`; a Linux build only Oscairn's
   [Linux allowlist](https://github.com/oscairn/oscairn/blob/main/docs/plugin-porting.md#the-linux-allowlist),
   no RPATH or RUNPATH, and no newer glibc or libstdc++ symbol version than the image's.
2. **plugin-check** plays each plugin with `oscairn-plugin-check`, built at `OSCAIRN_COMMIT`, on
   its platform's runner: `macos-15` for `macos-arm64`, `ubuntu-24.04-arm` for `linux-arm64`,
   `ubuntu-24.04` for `linux-x86_64`.
   Another platform is another row in `PLATFORMS` in the script. A release's build for a platform
   with no row (`windows-x86_64` today: Oscairn has no Windows host to load it in) is left out:
   it is not fetched, not in the archive and not in the record's `platforms`, and the run's
   summary, the candidate's `notice.txt` and the pull request say "<platform> build not imported:
   no Oscairn host for that platform yet". A release with no build left to import is refused.
3. **candidate** keeps the checked, unsigned candidate as the `unsigned-candidate` artifact: the
   builds, the license files, `plan.json` and `sign.sh`, the commands that sign it.
4. **sign**, only with the `OSCAIRN_PUBLISHER_KEY` secret, signs each build and the archive,
   writes `packages/<id>/<version>.json` and `.olib`, runs the checks Check runs, and opens a
   pull request. Without the key it does nothing, and the release is signed by hand:

   ```sh
   sh unsigned-candidate/sign.sh ~/oscairn/publisher.key oscairn-sign
   python3 scripts/import-release.py record --candidate unsigned-candidate --oscairn-sign oscairn-sign
   ```

   then a pull request with the two files `record` writes. Nothing is ever merged by the
   workflow.

The script runs the same way on any machine (`python3 scripts/import-release.py --help`), and
`scripts/test_import_release.py <oscairn-sign> [<oscairn-plugin-check>]` imports a release it
makes from the Arpeggiator's last build, served from a local folder and signed with a throwaway
key, and checks that a tampered hash, a changed license file and a build with an rpath are each
refused, and also covers `dependencies.packages` and `init` (the init library exception: both packages published, each of its conditions refused, a hostile archive refused before anything is written). Check runs it, with
`scripts/test_build_index.py <oscairn-sign>` (the index builder on a sample tree).

**The init library exception.** The rule that a new package id's first release is signed by the
owner by hand stands, and `import-release.py` imports only plugin releases. The owner has allowed
one exception, and only this one: **a plugin release's import also publishes that plugin's own init
library**, signed with the same publisher key under the same single approval, in the same pull
request. A host opens a saved Sound only with the exact plugin version it was saved with, so an init
library changes with every plugin release, and publishing each by hand would stop every release.
It applies when the release's manifest has `init` and every one of these holds, each refused with
a message naming it, with no plan or `sign.sh` made and nothing added here:

- **The id.** The library's `library.json` id is exactly `<the plugin package's id>.init`
  (`org.oscairn.sampler` may publish `org.oscairn.sampler.init` and no other id).
- **The plugin is not new.** Its package id already has a release here, whose publisher the
  library takes. A brand-new plugin's first release is still by hand, and so is its init library
  that once; the library of every later release is published by the import.
- **The publisher.** That publisher signs it, and every earlier record of the init library here
  is the same publisher's.
- **Its version.** The library's release is the plugin package's version, `dependencies.packages`
  names `<plugin>.init` at exactly that version, and the library's version is not here already
  (a version never changes once merged). It need not be here before: this one id, and no other
  package, may arrive in the same pull request.
- **Its license and source.** The record's license is `init.library.license` (an optional field of
  the manifest) or, when that is absent, the plugin's license; the archive's `library.json` must
  say the same, and a license other than the init library's last release's is refused unless the
  run accepts a license change. Its source is the plugin's (`url` and `revision`). The archive's
  `library.json` must state both and they must be equal to these: an archive with no license or
  no source, or another one, is refused. A text under `Licenses/` is required only when the license
  names a `LicenseRef-`, the rule for every package here; a plain SPDX id such as `CC0-1.0` needs none. Its license texts travel inside the archive (`Licenses/`), as the release provides them.
- **What it holds.** At most 8 MiB (the file and its entries added up) and 256 entries, read in
  place and never unpacked by the import: every entry a plain relative path under `Sounds/`,
  `Samples/`, `Waveforms/`, `Types/` or `Licenses/` (no `Plugins/`, no `Assets/`), no absolute or
  `..` path, no hidden file or backslash, no link, no executable (by mode or by Mach-O, ELF, PE or
  script header), no entry twice (a second `library.json` would be a second id), every entry listed
  in `library.json`'s `files` with its SHA-256, no plugin builds, no asset library, exactly one
  item, the Sound `init.sound`, whose nodes are the release's own plugins at the versions the
  release lists. Any package the library itself needs must be here already.
- **What is signed is what was checked.** plugin-check plays the release's init file as fetched.
  `sign` signs a copy of that same file with `oscairn-sign archive` (which reads it with the
  host's own reader and rewrites it with `library.sig` by the publisher's key), and both `sign` and
  `record` then compare the signed archive with that file: the same entries with the same bytes
  and the same manifest (the defaults the signing tool fills in for absent assets, dependencies and item signatures aside), `library.sig` aside; any difference refuses and records nothing.

The pull request lists both packages and says the library was published under this exception.
Nothing else is loosened: the exception covers neither another library, nor a library without a
plugin release, nor any other new package id.

**Signing in CI is the owner's choice.** To make it, create the environment `publisher-signing`
with required reviewers and deployments from `main` only, and put the publisher's whole key file
in it as the environment secret `OSCAIRN_PUBLISHER_KEY`, never as a repository secret. Only the
sign job's Sign step reads it, writing it to a file only it can read and removing it when the
step ends; no job that sees it runs plugin code. A pull request opened with the job's own token
starts no workflow, so Check would not run on it: give a fine-grained token with this
repository's contents and pull requests as `OSCAIRN_PR_TOKEN` in the same environment, or close
and reopen each such pull request.

### Keys

- **The root key** is made offline and stays offline with the maintainers. It signs only the
  index key.
- **The index key** is in the `OSCAIRN_INDEX_KEY` Actions secret. It signs the root
  (`repository.json`) on every publish.

Making them, on an offline machine:

```sh
oscairn-sign key root.key            # prints the root key's ID and public key
oscairn-sign key index.key           # the same for the index key
printf '%s\n' <index public key> > index-key.pub
oscairn-sign file root.key index-key.pub index-key.sig
```

Put the root public key in `repository-config.json` as `rootKey`, and the index public key and
the signature from `index-key.sig` as `indexKey`. Put the whole of `index.key` in the
`OSCAIRN_INDEX_KEY` secret, then delete that copy. **Replacing the index key** is the same with a
new `index.key`; add the old one to `revoked.json`'s `keys`.

### Setting up

See RFC 0031, "Hosting and cost". Protect `main`: pull requests only, with review from Code
Owners required, the Check workflow required, and no force pushes.

In `repository-config.json`, `blobBase` is where the pieces are (`<blobBase><ab>/<sha256>`, the
`blobs-<ab>` releases) and `exportDownload` where the ready-made archives are
(`<exportDownload><package ID>/<file>`, the `exports-<package ID>` releases); without
`exportDownload` none is made, and each version says so (`noExport`).
