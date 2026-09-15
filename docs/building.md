# Building and updating

## Build a wheel

```sh
uv build --wheel
```

The build downloads Unison's CLI, a watcher and their source archives, then checks
their SHA-256 hashes against `unison-artifacts.json`. Downloads happen during the
build, not when the package runs. The backend uses Python's standard library and
does not execute the downloaded programs.

Use `target` to build for a different platform:

```sh
uv build --wheel --config-setting target=windows_x86_64
uv build --wheel --config-setting target=macos_arm64
uv build --wheel --config-setting target=macos_x86_64
uv build --wheel --config-setting target=linux_x86_64
```

Supported targets are Linux x86-64, Windows x86-64, and macOS on Intel and Apple
Silicon. Other targets produce an error. Linux uses the upstream static build.
The macOS wheels also include
[autozimu/unison-fsmonitor](https://github.com/autozimu/unison-fsmonitor), since
Unison's macOS archives have no watcher. Linux and Windows use Unison's watcher.

The manifest records the wheel platform tags. The updater reads the minimum
macOS versions from the binaries. Linux wheels use `manylinux_2_17_x86_64`;
the builder and updater verify that both executables are x86-64 ELF files with
no dynamic loader or dynamic linking section. Building a wheel for another
platform does not test whether it runs there.

`uv build --sdist` produces a source distribution with the builder and manifest.
A wheel built from it follows the same download and checksum checks.

## Update Unison and the watcher

```sh
uv run python -m tools.update_binaries
uv sync
uv run pytest
uv build
```

With no arguments, the updater selects the latest stable releases of both tools.
Use `--unison VERSION` or `--watcher VERSION` to select one release and leave the
other unchanged. `--check` performs the checks without changing files.

The updater downloads all supported binaries and sources, checks release-asset
hashes, and refreshes the manifest and watcher license notices. The runtime reads
its expected Unison version from the installed manifest, so no version edits in
Python code or documentation are needed.

Review the resulting diff and run the tests. The updater checks known archive
formats and reports changed licenses or missing notices. It does not determine
license compatibility or establish that a new release behaves like the old one.
It refuses to replace the recorded bytes for an existing release silently.

## Offline builds

The updater can save its verified downloads:

```sh
uv run python -m tools.update_binaries --artifact-dir /path/to/artifacts
uv build --wheel --config-setting artifact-dir=/path/to/artifacts
```

Each archive is stored under its SHA-256 digest. A build needs the selected
Unison binary and source, plus the watcher binary and source for macOS. Offline
inputs receive the same checks as downloads.

To run the test that builds all four wheels, set `MONOPHONY_ARTIFACT_DIR` to a
populated artifact directory before running `uv run pytest`. The test is skipped
when this variable is absent.

## License notices

`uv run python -m tools.watcher_licenses` refreshes the watcher dependency notices
without updating the binaries. It uses the watcher's pinned `Cargo.lock`, checks
crate downloads against that lock, and collects their license files. It needs no
Python dependencies beyond those provided by the standard library.

Normal builds use the notices already in `third_party`. See
[redistribution](redistribution.md) for their purpose and limitations.

## Testing another Unison build

Set `MONOPHONY_UNISON` to an executable path. Its reported version must match the
installed manifest. Put its matching watcher beside it. The test suite and CLI
both honor this override.

## GitHub workflows

CI builds and tests wheels on Linux x86-64, Windows x86-64, and macOS Intel and
Apple Silicon, with Python 3.11 and 3.14. Each wheel is built from the source
archive and installed before the tests run. The tests include live syncing.
Run `uv run actionlint` to check workflow syntax locally; the linter is in the
development dependency group.

**Update bundled binaries** runs weekly or from the Actions tab. It opens a PR
with new binaries, checksums and notices, then runs CI on that commit. Manual
runs accept optional Unison and watcher versions, with the same selection rules
as the command above. Enable **Allow GitHub Actions to create and approve pull
requests** in the repository's Actions settings. The workflow uses GitHub's
built-in token; because its PRs do not trigger ordinary PR workflows, the tests
appear under the update workflow run. Dependabot handles GitHub Actions and
Python development dependency updates separately.

**Publish** runs when a GitHub release is published. Its tag must be `v` followed
by the package version in `pyproject.toml`. It builds and tests all four wheels,
then uploads those wheels and the source archive to PyPI with `uv publish`.
To release, update the package version with `uv version`, commit the version and
lockfile changes, and publish a GitHub release at that commit with the matching tag.

Before the first release:

1. Create a GitHub environment named `pypi`.
2. Add a [PyPI trusted publisher](https://docs.pypi.org/trusted-publishers/adding-a-publisher/)
   for this repository, workflow `publish.yml`, environment `pypi`. For a new
   project, use PyPI's pending publisher form with project name `monophony`.

No PyPI API token is needed. These workflows take effect once the repository is
on GitHub; local builds need no GitHub or PyPI configuration.
