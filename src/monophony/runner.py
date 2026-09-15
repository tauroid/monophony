"""Conservative Unison argument handling and subprocess lifecycle."""

import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path

from .errors import TranslationError
from .gitignore import ignore_arguments

# These preferences affect presentation, connection, scheduling or metadata;
# they cannot reopen an ignored subtree or change pathname interpretation.
BOOLEAN_OPTIONS = frozenset(
    [
        "batch",
        "auto",
        "silent",
        "terse",
        "times",
        "owner",
        "group",
        "acl",
        "xattrs",
        "copyonconflict",
        "confirmbigdel",
        "confirmmerge",
        "dontchmod",
        "numericids",
        "sortbysize",
        "sortnewfirst",
        "dumbtty",
        "rsync",
        "killserver",
        "addversionno",
        "contactquietly",
        "log",
    ]
)
VALUE_OPTIONS = frozenset(
    [
        "perms",
        "retry",
        "color",
        "servercmd",
        "sshargs",
        "sshcmd",
        "logfile",
        "maxerrors",
        "maxthreads",
        "sortfirst",
        "sortlast",
        "diff",
    ]
)
FIXED_OPTIONS = {"ignorecase": "false", "unicode": "false", "ui": "text"}


@dataclass(frozen=True)
class Invocation:
    roots: tuple[str, str]
    local_roots: tuple[Path, ...]
    options: tuple[str, ...]


def parse_arguments(arguments):
    args = list(arguments)
    roots, positional, options = [], [], []
    repeat = None
    watch = True
    i = 0
    while i < len(args):
        arg = args[i]
        if not isinstance(arg, str) or "\0" in arg:
            raise TranslationError("Arguments must be strings without NUL characters")
        if not arg.startswith("-"):
            positional.append(arg)
            i += 1
            continue
        name, equal, attached = arg[1:].partition("=")
        if name in BOOLEAN_OPTIONS:
            if equal and attached not in ("true", "false"):
                raise TranslationError(f"{arg}: expected a boolean")
            options.append(arg)
        elif name == "watch":
            if equal and attached not in ("true", "false"):
                raise TranslationError("-watch expects true or false")
            watch = not equal or attached == "true"
        elif name in VALUE_OPTIONS | FIXED_OPTIONS.keys() | {"root", "repeat"}:
            if equal:
                value = attached
            else:
                i += 1
                if i == len(args):
                    raise TranslationError(f"{arg} requires a value")
                value = args[i]
            if not isinstance(value, str) or "\0" in value:
                raise TranslationError(f"Invalid value for {arg}")
            if name == "root":
                roots.append(value)
            elif name in FIXED_OPTIONS:
                if value != FIXED_OPTIONS[name]:
                    raise TranslationError(
                        f"{arg} must be {FIXED_OPTIONS[name]!r} for verified translation"
                    )
            elif name == "repeat":
                if value and not re.fullmatch(r"(?:watch(?:\+\d+)?|\d+)", value):
                    raise TranslationError(
                        "-repeat expects watch, watch+SECONDS, SECONDS or an empty string"
                    )
                repeat = value
                options.extend(["-" + name, value])
            else:
                options.extend(["-" + name, value])
        else:
            raise TranslationError(
                f"{arg} is not verified for ignore translation. Profiles, extra ignore rules, "
                "-path, -follow, -force and unknown preferences are intentionally blocked."
            )
        i += 1
    if roots and positional:
        raise TranslationError("Use two positional roots or two -root options, without a profile")
    roots = roots or positional
    if len(roots) != 2:
        raise TranslationError("Expected two explicit roots; Unison profiles are not supported")
    local = []
    normalized = []
    for root in roots:
        if root.startswith(("ssh://", "socket://")):
            normalized.append(root)
        elif "://" in root or (":" in root and not re.match(r"^[A-Za-z]:[\\/]", root)):
            raise TranslationError("Use an explicit local path or an ssh:// or socket:// root")
        else:
            path = Path(root).expanduser().resolve(strict=True)
            if not path.is_dir():
                raise TranslationError(f"Local root is not a directory: {path}")
            local.append(path)
            normalized.append(str(path))
    if not local:
        raise TranslationError("At least one local root is required to read .gitignore files")
    if len(local) == 2 and (
        local[0] == local[1] or local[0] in local[1].parents or local[1] in local[0].parents
    ):
        raise TranslationError("Local roots must be distinct, non-overlapping directories")
    if repeat is not None and repeat.startswith("watch") and not watch:
        raise TranslationError("-watch=false conflicts with -repeat watch")
    if repeat is None and watch:
        options.extend(["-repeat", "watch"])
    options.append("-watch=" + str(watch).lower())
    for name, value in FIXED_OPTIONS.items():
        options.extend(["-" + name, value])
    return Invocation(tuple(normalized), tuple(local), tuple(options))


def executable():
    """Locate the bundled binary, or an explicitly selected development binary."""
    vendor = Path(__file__).parent / "_vendor"
    if not vendor.is_dir():
        try:
            vendor = Path(distribution("monophony").locate_file("monophony/_vendor"))
        except PackageNotFoundError as exc:
            raise TranslationError("Install monophony with uv sync before running it") from exc
    expected_version = json.loads((vendor / "unison-artifacts.json").read_text())["version"]
    override = os.environ.get("MONOPHONY_UNISON")
    if override:
        path = Path(override).expanduser().resolve(strict=True)
    else:
        candidates = list((vendor / "unison").rglob("unison.exe" if os.name == "nt" else "unison"))
        candidates = [p for p in candidates if p.is_file()]
        if len(candidates) != 1:
            raise TranslationError(
                "Bundled Unison missing: install a built wheel, or set MONOPHONY_UNISON"
            )
        path = candidates[0]
    result = subprocess.run(
        [str(path), "-version"], check=False, capture_output=True, text=True, timeout=10
    )
    if result.returncode or not re.search(
        r"\bunison version " + re.escape(expected_version) + r"(?:\s|$)", result.stdout
    ):
        raise TranslationError(
            f"Expected Unison {expected_version}: {result.stdout.strip()} {result.stderr.strip()}"
        )
    return path


def sync(*arguments: str) -> int:
    """Run monophony with the same argument tokens as its CLI; return Unison's status.

    Example: sync('/work/project', 'ssh://host//work/project', '-batch').
    Pass '-repeat', '' for one synchronization. Translation errors are raised
    before Unison starts. Ignore files are captured once at startup.
    """
    if arguments in (("-version",), ("-help",)):
        return subprocess.call([str(executable()), *arguments])
    invocation = parse_arguments(arguments)
    generated = ignore_arguments(invocation.local_roots)
    binary = executable()
    env = os.environ.copy()
    env["PATH"] = str(binary.parent) + os.pathsep + env.get("PATH", "")
    # Explicit roots still load default.prf in Unison. Select our own empty
    # profile so unchecked preferences cannot silently enter the invocation.
    state = Path(env.get("UNISON", str(Path.home() / ".unison"))).resolve()
    state.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="monophony-", dir=state) as directory:
        profile = Path(directory) / "monophony.prf"
        profile.write_text("# Managed by monophony; ignore rules are command-line preferences.\n")
        command = [
            str(binary),
            profile.relative_to(state).as_posix(),
            "-root",
            invocation.roots[0],
            "-root",
            invocation.roots[1],
            *invocation.options,
            *generated,
        ]
        if sum(len(arg.encode("utf-8")) + 3 for arg in command) > 28_000:
            raise TranslationError("Invocation exceeds the portable command-line size limit")
        process = subprocess.Popen(command, env=env)
        try:
            return process.wait()
        except KeyboardInterrupt:
            # A terminal Ctrl-C also reaches the child on supported platforms.
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            return 130
