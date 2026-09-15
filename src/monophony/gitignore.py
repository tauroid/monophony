"""Git patterns -> typed path languages -> a verified Unison predicate."""

import os
from dataclasses import dataclass
from pathlib import Path

from . import language as L
from .errors import TranslationError

MAX_RULES = 128
MAX_IGNORE_FILES = 64


@dataclass(frozen=True)
class Rule:
    pattern: tuple
    include: bool
    directory_only: bool
    origin: str


@dataclass(frozen=True)
class Policy:
    files: tuple
    directories: tuple
    includes: tuple
    directory_includes: tuple
    rules: tuple[Rule, ...]

    def ignored(self, path: str, *, directory=False):
        return L.matches(self.directories if directory else self.files, path)


def glob(pattern: str):
    """Git wildmatch's ASCII subset; unsupported constructs fail explicitly."""
    if len(pattern) > 512:
        raise TranslationError("Patterns longer than 512 characters are not supported")
    if not pattern.isascii() or any(ord(c) < 32 or ord(c) == 127 for c in pattern):
        raise TranslationError("Patterns must contain printable ASCII characters")
    result = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if c == "\\":
            i += 1
            if i == len(pattern):
                return L.EMPTY  # Git's dangling escape never matches.
            if pattern[i] == "/":
                raise TranslationError("Escaped separators are not supported")
            result.append(L.literal(pattern[i]))
        elif c == "*":
            end = i
            while end < len(pattern) and pattern[end] == "*":
                end += 1
            boundary = i == 0 or pattern[i - 1] == "/"
            if end - i == 2 and boundary and end < len(pattern) and pattern[end] == "/":
                result.append(L.star(L.seq(L.COMPONENT, L.SLASH)))
                i = end
            elif end - i == 2 and boundary and end == len(pattern) and i > 0:
                result.append(L.PATHS)
                i = end - 1
            else:
                if end - i > 2:
                    raise TranslationError("Runs of more than two '*' characters are not supported")
                result.append(L.star(L.COMPONENT_CHAR))
                i = end - 1
        elif c == "?":
            result.append(L.COMPONENT_CHAR)
        elif c == "[":
            j = i + 1
            negative = j < len(pattern) and pattern[j] in "!^"
            if negative:
                j += 1
            values = set()
            first = True
            while j < len(pattern) and (first or pattern[j] != "]"):
                first = False
                if pattern[j] == "[" or pattern[j] == "\\":
                    raise TranslationError(
                        "Nested/POSIX classes and escapes inside classes are not supported"
                    )
                start = ord(pattern[j])
                if j + 2 < len(pattern) and pattern[j + 1] == "-" and pattern[j + 2] != "]":
                    end = ord(pattern[j + 2])
                    if end < start:
                        raise TranslationError("Descending character ranges are not supported")
                    values.update(range(start, end + 1))
                    j += 3
                else:
                    values.add(start)
                    j += 1
            if j == len(pattern):
                raise TranslationError("Unclosed character class")
            values = L.ALPHABET - values if negative else values
            result.append(L.chars(values - {47}))
            i = j
        else:
            result.append(L.literal(c))
        i += 1
    return L.seq(*result)


def parse(text: str, *, scope="", source=".gitignore"):
    rules = []
    for number, line in enumerate(text.split("\n"), 1):
        line = line.removesuffix("\r")
        # A trailing space is literal only when preceded by an odd escape count.
        while line.endswith(" "):
            preceding = line[:-1]
            escapes = len(preceding) - len(preceding.rstrip("\\"))
            if escapes % 2:
                break
            line = preceding
        if not line or line.startswith("#"):
            continue
        origin = f"{source}:{number}"
        include = line.startswith("!")
        if include:
            line = line[1:]
        if not line:
            continue
        directory_only = line.endswith("/")
        if directory_only:
            line = line[:-1]
        anchored = "/" in line
        line = line.removeprefix("/")
        if not line:
            continue
        try:
            if "//" in line:
                raise TranslationError("Repeated separators are not supported")
            pattern = glob(line)
            if not anchored:
                pattern = L.seq(L.star(L.seq(L.COMPONENT, L.SLASH)), pattern)
            if scope:
                pattern = L.seq(L.literal(scope), L.SLASH, pattern)
            pattern = L.intersection(pattern, L.PATHS)
        except TranslationError as exc:
            raise TranslationError(f"{origin}: {exc}") from exc
        rules.append(Rule(pattern, include, directory_only, origin))
    return rules


def policy(rules):
    if len(rules) > MAX_RULES:
        raise TranslationError(f"More than {MAX_RULES} ignore rules")
    files = directories = includes = directory_includes = L.EMPTY
    for rule in rules:
        if rule.include:
            directories = L.difference(directories, rule.pattern)
            directory_includes = L.union(directory_includes, rule.pattern)
            if not rule.directory_only:
                files = L.difference(files, rule.pattern)
                includes = L.union(includes, rule.pattern)
        else:
            directories = L.union(directories, rule.pattern)
            directory_includes = L.difference(directory_includes, rule.pattern)
            if not rule.directory_only:
                files = L.union(files, rule.pattern)
                includes = L.difference(includes, rule.pattern)
    parents = L.descendants(directories)
    return Policy(
        L.union(files, parents),
        L.union(directories, parents),
        L.difference(includes, parents),
        L.difference(directory_includes, parents),
        tuple(rules),
    )


def discover(root: Path):
    """Read nested ignores, never descend through symlinks or ignored directories.

    Directory enumeration discovers configuration only. Existing file names/types
    never enter the compiled language, which also covers future/remote paths.
    """
    root = Path(root)
    rules = []
    count = 0
    pending = [(root, "")]
    while pending:
        directory, scope = pending.pop()
        if scope and policy(rules).ignored(scope, directory=True):
            continue
        ignore = directory / ".gitignore"
        if not ignore.is_symlink() and ignore.exists():
            count += 1
            if count > MAX_IGNORE_FILES:
                raise TranslationError(f"More than {MAX_IGNORE_FILES} .gitignore files")
            with ignore.open("rb") as stream:
                data = stream.read(1_048_577)
            if len(data) > 1_048_576:
                raise TranslationError(f"{ignore}: larger than 1 MiB")
            try:
                text = data.decode("utf-8-sig")
            except UnicodeError as exc:
                raise TranslationError(f"{ignore}: expected UTF-8") from exc
            rules.extend(parse(text, scope=scope, source=str(ignore)))
            if len(rules) > MAX_RULES:
                raise TranslationError(f"More than {MAX_RULES} ignore rules")
        with os.scandir(directory) as entries:
            children = sorted(
                (
                    entry.name
                    for entry in entries
                    if entry.name != ".git" and entry.is_dir(follow_symlinks=False)
                ),
                reverse=True,
            )
        pending.extend(
            (directory / name, f"{scope}/{name}" if scope else name) for name in children
        )
    return policy(rules)


def compile_policies(policies):
    policies = list(policies)
    for i, left in enumerate(policies):
        for right in policies[i + 1 :]:
            conflict = L.union(
                L.intersection(left.files, right.includes),
                L.intersection(right.files, left.includes),
                L.intersection(left.directories, right.directory_includes),
                L.intersection(right.directories, left.directory_includes),
            )
            example = L.witness(conflict)
            if example is not None:
                origins = "\n".join(rule.origin for p in (left, right) for rule in p.rules)
                raise TranslationError(
                    f"Contradictory root policies at {example!r}; rules:\n{origins}"
                )
    files = L.union(*(p.files for p in policies))
    directories = L.union(*(p.directories for p in policies))
    example = L.witness(L.difference(files, directories))
    if example is not None:
        raise TranslationError(
            f"Directory-only inclusion cannot be lowered safely at {example!r}: "
            "Unison would prune an included directory to exclude a same-named file"
        )
    result = L.regex(files)
    return [] if result is None else ["-ignore", "Regex " + result]


def ignore_arguments(roots):
    try:
        return compile_policies(discover(Path(root)) for root in roots)
    except RecursionError as exc:
        raise TranslationError("Ignore language exceeds compiler nesting limits") from exc
