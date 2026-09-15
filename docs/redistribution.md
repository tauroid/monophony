# Redistribution

Monophony and Unison are licensed under GPL-3.0-or-later. Unison is copyright
Benjamin C. Pierce and contributors. The macOS watcher,
`autozimu/unison-fsmonitor`, uses the MIT license.

GPLv3 permits distributing Unison's executable subject to its conditions,
including providing corresponding source. Monophony includes that source in
each wheel. This is the chosen distribution method; the GPL permits other
methods too.

## Files included in a wheel

Under `monophony/_vendor/`:

| Path | Contents |
| --- | --- |
| `unison/bin/` | CLI and watcher executables |
| `source/unison.tar.gz` | Unison source, build scripts and upstream notices |
| `source/monophony.tar.gz` | Monophony source, builder and tests |
| `source/unison-fsmonitor.tar.gz` | Watcher source and Cargo.lock; macOS wheels only |
| `LICENSE-Unison` | GPL license text |
| `licenses/` | Runtime and dependency license notices |
| `unison-artifacts.json` | Release URLs, versions and SHA-256 hashes |

Keep these files when redistributing a wheel. The upstream executables are
unmodified. The package includes no GUI or GTK libraries. The selected Windows
executables import system DLLs only.

The OCaml and GCC runtime licenses include linking exceptions. The static Linux
build also includes musl code. Their notices are retained alongside notices for
the macOS watcher's Rust runtime and dependencies.

## What the scripts check

The build checks downloaded bytes against the recorded hashes. Binary hashes
come from upstream release metadata; source hashes are recorded by the updater.
These checks detect changed downloads. They do not independently authenticate
the publisher.

The updater collects licenses and notices and flags changes or missing files.
**It does not determine license compatibility or certify compliance.** New
licenses, changed dependencies and additional bundled programs need review.
Some collected notices cover dependencies that are not linked into every target.

## References

The source archives contain the notices for the bundled releases. Current
upstream documents are also available here:

- [Unison license](https://github.com/bcpierce00/unison/blob/master/LICENSE)
- [Unison source notice](https://github.com/bcpierce00/unison/blob/master/src/pred.ml)
- [GPLv3, section 6](https://www.gnu.org/licenses/gpl-3.0.html#section6)
- [OCaml license and linking exception](https://github.com/ocaml/ocaml/blob/trunk/LICENSE)
- [musl copyright and licenses](https://git.musl-libc.org/cgit/musl/tree/COPYRIGHT)
- [MinGW-w64 license](https://github.com/mingw-w64/mingw-w64/blob/master/COPYING)
- [GCC runtime exception](https://github.com/gcc-mirror/gcc/blob/master/COPYING.RUNTIME)
- [macOS watcher license](https://github.com/autozimu/unison-fsmonitor/blob/master/LICENSE.txt)
- [Rust copyright](https://github.com/rust-lang/rust/blob/main/COPYRIGHT)
