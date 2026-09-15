# monophony

**Warning: this package is in alpha state and almost all is AI-written. In the end it just invokes Unison, but any bugs in the way it does so are the user's risk to assume. I am not affiliated with the Unison project in any way; this package just fulfils a specific need I have.**

`monophony` is essentially just a Python wrapper for [Unison](https://github.com/bcpierce00/unison) oriented toward the task of live syncing a Git repo with a remote.

The main contribution is a converter from `.gitignore` files into Unison `-ignore` (/ `-ignorenot`) arguments, which makes it simple to use on projects set up for Git, without requiring extra Unison config.

This is useful, for example, when working on code locally while simultaneously testing it in a remote environment.

`-repeat watch` is on by default but can be disabled with `-watch=false`.

This package will use a bundled version of Unison rather than any other installed version.

## Usage

### CLI

```sh
monophony ./left ./right
monophony ./project ssh://host//srv/project

# Sync once.
monophony ./left ./right -watch=false
```

The CLI accepts many of the same arguments as bare Unison, but some may be disallowed if they conflict (or have potential to conflict) with the `.gitignore` behaviour. Significantly, profiles are disabled.

The [full option list](src/monophony/runner.py) is in the argument parser.

### Python

```python
from monophony import sync

status = sync("./left", "./right")
```

`sync` takes the same arguments as the CLI.

## Caveats

`.gitignore` and `-ignore` / `-ignorenot` are not a perfect match. This arises from the fact Unison operates only on a path whereas Git distinguishes between files and directories when applying rules. So if Unison's presented with e.g. a directory rename from `a` to `b` (with `b` ignored by a `b/` rule) we can't create an `-ignore` that will exclude it but not exclude a similar rename of a _file_ from `a` to `b`. So empty directories may unexpectedly sync (but not their contents).

There are other points of incompatibility between Git and Unison - these should hopefully be picked up on start and produce an error. See [conversion details and limits](docs/semantics.md) for (AI) technical details about the conversion.

Other caveats:
- Matching is case-sensitive, regardless of Git's `core.ignoreCase` setting.
- Tracked files that would be gitignored if not tracked, are ignored.
- There is no thought given to preservation of ignored files - Unison won't propagate change information about them, but they could still be deleted if e.g. the (non-ignored) parent folder is deleted on the remote (or vice versa).
- Changes to .gitignore files will need a restart to register.

## Development

See [building and updating](docs/building.md) for building wheels for other platforms and updating the bundled binaries, and [redistribution](docs/redistribution.md) for included sources and licenses.
