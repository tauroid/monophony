# Compiler design

The compiler represents sets of relative paths as regular languages over bytes.
Components are nonempty, contain neither NUL nor `/`, and are not `.` or `..`.
Git and Unison both match pathnames at byte level in the selected case-sensitive,
mode without Unicode normalization. Patterns are limited to ASCII, but wildcard
matches can contain any filename bytes.

The intermediate representation (IR) consists of the empty set, the empty string, character sets, union,
concatenation, Kleene star (zero or more concatenations), and set difference.
Intersection is derived as `A ∩ B = A \ (A \ B)`.

## Interpreting rules

Each rule has a scoped path language, an include/exclude decision, a directory-only
flag, and a source file/line. Its language matches the entry itself, not implicitly
all descendants. Rules are processed in Git precedence order: outer files first,
inner files next, lines in source order. Sibling scopes are disjoint, so their
relative ordering has no effect.

Maintain four languages: direct file exclusions `F`, direct directory exclusions
`D`, explicit file inclusions `IF`, and explicit directory inclusions `ID`.

- Exclude `R`: union `R` into the appropriate exclusion sets and subtract it from
  their explicit inclusion sets.
- Include `R`: subtract `R` from exclusion sets and union it into inclusion sets.
- Directory-only rules affect only `D` and `ID`.

After resolving direct decisions, let `P = D · '/' · Paths`, the language of
descendants of excluded directories. Effective decisions are:

```text
file exclusions      = F ∪ P
directory exclusions = D ∪ P
explicit file includes      = IF \ P
explicit directory includes = ID \ P
```

This is why `build/` followed by `!build/keep` still excludes `build/keep`.
An ignored ancestor is evaluated as a directory even when the final target is a
file. Nested ignore discovery skips excluded directories, and does not follow
symlinks. It skips `.git` administration directories for discovery only.

## Combining roots

For every pair of roots, intersect each root's effective exclusion set with the
other's effective explicit inclusion set, separately for files and directories.
Nonempty intersection is a contradiction. An accepting path from the automaton
is reported as a witness, even if it exists on neither machine.

If there is no contradiction, union the roots' file exclusion languages and
union their directory exclusion languages. An unspecified decision has no
opinion; it is not an explicit inclusion.

## Conversion to Unison

Let the resulting languages be `F*` and `D*`. Unison only receives pathnames and
prunes ignored directories. Require `F* \ D*` to be empty. Otherwise Unison would
need to ignore an ordinary file but traverse a directory with the identical
pathname, and lowering fails with a witness.

Use `F*` as Unison's ignore predicate. Any directory pruned by it is excluded by
the directory semantics. Directories in `D* \ F*` may themselves synchronize,
but their descendants are excluded. Files and symlinks have the same ignore
decisions as the combined policy. The directory entry itself may synchronize,
as described in the README.

Deleting or replacing a parent can still remove ignored descendants. This check
only establishes which paths the ignore predicate excludes. Tracked files
receive the same treatment as other files.

## Automata and regex output

Brzozowski derivatives construct a deterministic finite automaton (DFA).
Character predicates partition the byte alphabet, so transitions are computed
once for each equivalent byte class. Reachable accepting states decide emptiness;
breadth-first traversal supplies an example string.

For output, minimize the DFA, remove states that cannot reach acceptance, and
eliminate states to obtain an ordinary regex. Unison internally has set operations
but its textual regex parser does not expose them. The generated regex therefore
contains only constructs accepted by that parser. Unison matches the entire
pathname, so no line-sensitive `^`/`$` anchors are added.

Compilation stops at these limits, even if a larger translation would be possible:

- 512 characters per pattern
- 128 rules across all ignore files in a root
- 64 ignore files per root
- 256 DFA states per operation
- 24,000 characters in the output regex

Unsupported syntax produces an error with the source file and line number.

## Tests

`tests/test_semantics.py` compares the typed interpreter against Git, including
ordered generated cases. `tests/test_integration.py` checks the lowered predicate
through actual Unison transfers, future file creation under its watcher,
symlinks, files present only remotely, and deletion of folders with ignored contents.

Primary references:

- [Git ignore specification](https://git-scm.com/docs/gitignore)
- [Unison predicate implementation](https://github.com/bcpierce00/unison/blob/master/src/pred.ml)
- [Unison regex parser](https://github.com/bcpierce00/unison/blob/master/src/ubase/rx.ml)
- [Unison reconciliation and deletion checks](https://github.com/bcpierce00/unison/blob/master/src/recon.ml)
