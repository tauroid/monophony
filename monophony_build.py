"""Dependency-free PEP 517 backend, invoked with `uv build`.

Builds platform wheels without executing target code. Release archives and source
are pinned by SHA-256; only the CLI and watcher are extracted, never GUI DLLs.
"""

import base64
import csv
import gzip
import hashlib
import io
import json
import os
import platform
import stat
import struct
import tarfile
import tempfile
import tomllib
import zipfile
from pathlib import Path, PurePosixPath
from urllib.request import urlopen

ROOT = Path(__file__).parent
PROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
VERSION = PROJECT["version"]
DIST = f"monophony-{VERSION}.dist-info"
LOCK = json.loads((ROOT / "unison-artifacts.json").read_text())
MAX_DOWNLOAD = 128 * 1024 * 1024


def settings(config_settings=None):
    config = dict(config_settings or {})
    unknown = config.keys() - {"target", "unison-version", "artifact-dir"}
    if unknown:
        raise ValueError(f"Unknown build settings: {sorted(unknown)}")
    if any(not isinstance(value, str) for value in config.values()):
        raise ValueError("Build settings must each have one string value")
    version = config.get("unison-version", LOCK["version"])
    if version != LOCK["version"]:
        raise ValueError(
            f"Unison {version} is not pinned; run uv run python -m tools.update_binaries first"
        )
    machine = platform.machine().lower()
    host = {
        ("linux", "x86_64"): "linux_x86_64",
        ("darwin", "arm64"): "macos_arm64",
        ("darwin", "x86_64"): "macos_x86_64",
        ("windows", "amd64"): "windows_x86_64",
        ("windows", "x86_64"): "windows_x86_64",
    }
    target = config.get("target", host.get((platform.system().lower(), machine)))
    if target not in LOCK["targets"]:
        raise ValueError(
            f"Unsupported target {target!r}; choose one of {', '.join(LOCK['targets'])}"
        )
    return config, target


def fetch(spec, artifact_dir=None):
    """Verify before opening an archive, including pre-populated offline artifacts."""
    digest = spec["sha256"]
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError("Invalid SHA-256 pin")
    if artifact_dir:
        path = Path(artifact_dir) / digest
        with path.open("rb") as stream:
            data = stream.read(MAX_DOWNLOAD + 1)
    else:
        if not spec["url"].startswith("https://"):
            raise ValueError("Artifacts must use HTTPS")
        with urlopen(spec["url"], timeout=60) as response:
            data = response.read(MAX_DOWNLOAD + 1)
    if len(data) > MAX_DOWNLOAD:
        raise ValueError("Artifact exceeds size limit")
    actual = hashlib.sha256(data).hexdigest()
    if actual != digest:
        raise ValueError(f"SHA-256 mismatch for {spec['url']}: expected {digest}, got {actual}")
    return data


def binaries(data, windows=False, wanted=None):
    if wanted is None:
        wanted = (
            {"unison.exe", "unison-fsmonitor.exe"} if windows else {"unison", "unison-fsmonitor"}
        )
    result = {}

    def accept(name, read, regular):
        path = PurePosixPath(name)
        if path.name not in wanted:
            return
        if path.is_absolute() or ".." in path.parts or "\\" in name or not regular:
            raise ValueError(f"Unsafe binary archive member {name!r}")
        if path.name in result:
            raise ValueError(f"Duplicate binary {path.name}")
        payload = read()
        if len(payload) > MAX_DOWNLOAD:
            raise ValueError("Binary exceeds size limit")
        result[path.name] = payload

    if windows:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for member in archive.infolist():
                if member.file_size > MAX_DOWNLOAD:
                    raise ValueError("Oversized archive member")
                mode = member.external_attr >> 16
                accept(
                    member.filename,
                    lambda m=member: archive.read(m),
                    not member.is_dir() and not stat.S_ISLNK(mode),
                )
    else:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
            for member in archive:
                if member.size > MAX_DOWNLOAD:
                    raise ValueError("Oversized archive member")
                accept(member.name, lambda m=member: archive.extractfile(m).read(), member.isfile())
    if result.keys() != wanted:
        raise ValueError(f"Archive lacks required executables: {wanted - result.keys()}")
    return result


def check_static_linux(programs):
    """The manylinux tag relies on standalone x86-64 executables, with no loader."""
    for name, data in programs.items():
        if len(data) < 64 or data[:7] != b"\x7fELF\x02\x01\x01":
            raise ValueError(f"{name}: expected a little-endian ELF64 executable")
        kind, machine = struct.unpack_from("<HH", data, 16)
        offset = struct.unpack_from("<Q", data, 32)[0]
        size, count = struct.unpack_from("<HH", data, 54)
        if kind != 2 or machine != 62 or size != 56 or not count:
            raise ValueError(f"{name}: expected a static x86-64 executable")
        if offset < 64 or offset + size * count > len(data):
            raise ValueError(f"{name}: invalid ELF program headers")
        types = {struct.unpack_from("<I", data, offset + size * i)[0] for i in range(count)}
        if 1 not in types or types & {2, 3}:  # PT_LOAD, PT_DYNAMIC, PT_INTERP
            raise ValueError(f"{name}: Linux binaries must be statically linked")


def source_files():
    fixed = [
        "pyproject.toml",
        "monophony_build.py",
        "unison-artifacts.json",
        "README.md",
        "LICENSE",
    ]
    paths = [ROOT / name for name in fixed]
    if (ROOT / "uv.lock").exists():
        paths.append(ROOT / "uv.lock")
    for directory in ("src", "tests", "docs", "tools", "third_party", ".github"):
        paths.extend(
            p
            for p in (ROOT / directory).rglob("*")
            if p.is_file()
            and "__pycache__" not in p.parts
            and p.suffix != ".pyc"
            and "_vendor" not in p.parts
        )
    return sorted(paths)


def metadata():
    return (
        f"Metadata-Version: 2.4\nName: monophony\nVersion: {VERSION}\n"
        f"Summary: {PROJECT['description']}\nRequires-Python: {PROJECT['requires-python']}\n"
        "License-Expression: GPL-3.0-or-later\nLicense-File: LICENSE\n"
        "Description-Content-Type: text/markdown\n\n" + (ROOT / "README.md").read_text()
    ).encode()


def build_sdist(sdist_directory, config_settings=None):
    filename = f"monophony-{VERSION}.tar.gz"
    destination = Path(sdist_directory)
    destination.mkdir(parents=True, exist_ok=True)
    # Stable gzip header, tar timestamps/ownership and sorted entries.
    with (destination / filename).open("wb") as output:
        with gzip.GzipFile(filename="", fileobj=output, mode="wb", mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w") as archive:
                for path in source_files():
                    data = path.read_bytes()
                    info = tarfile.TarInfo(
                        f"monophony-{VERSION}/{path.relative_to(ROOT).as_posix()}"
                    )
                    info.size, info.mode = len(data), 0o644
                    archive.addfile(info, io.BytesIO(data))
                info = tarfile.TarInfo(f"monophony-{VERSION}/PKG-INFO")
                data = metadata()
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
    return filename


def metadata_files(target):
    tag = f"py3-none-{LOCK['targets'][target]['wheel_tag']}"
    return {
        f"{DIST}/METADATA": metadata(),
        f"{DIST}/WHEEL": f"Wheel-Version: 1.0\nGenerator: monophony_build\nRoot-Is-Purelib: false\nTag: {tag}\n".encode(),
        f"{DIST}/entry_points.txt": b"[console_scripts]\nmonophony = monophony.cli:main\n",
        f"{DIST}/licenses/LICENSE": (ROOT / "LICENSE").read_bytes(),
    }


def prepare_metadata_for_build_wheel(metadata_directory, config_settings=None):
    _, target = settings(config_settings)
    for name, data in metadata_files(target).items():
        path = Path(metadata_directory) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return DIST


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    return _build_wheel(wheel_directory, config_settings)


def build_editable(wheel_directory, config_settings=None, metadata_directory=None):
    return _build_wheel(wheel_directory, config_settings, editable=True)


prepare_metadata_for_build_editable = prepare_metadata_for_build_wheel


def _build_wheel(wheel_directory, config_settings=None, *, editable=False):
    config, target = settings(config_settings)
    spec = LOCK["targets"][target]
    # The build host never runs these executables, so target selection is genuine
    # cross packaging, independent of the host Python's platform tag.
    programs = binaries(
        fetch(spec, config.get("artifact-dir")),
        target.startswith("windows"),
        wanted={"unison"} if "watcher" in spec else None,
    )
    if target == "linux_x86_64":
        check_static_linux(programs)
    if "watcher" in spec:
        programs.update(
            binaries(
                fetch(spec["watcher"], config.get("artifact-dir")), wanted={"unison-fsmonitor"}
            )
        )
    source = fetch(LOCK["source"], config.get("artifact-dir"))
    payloads = {
        p.relative_to(ROOT / "src").as_posix(): p.read_bytes()
        for p in source_files()
        if p.is_relative_to(ROOT / "src") and not editable
    }
    if editable:
        payloads["_monophony_editable.pth"] = f"{ROOT / 'src'}\n{ROOT}\n".encode()
    payloads.update(metadata_files(target))
    for name, data in programs.items():
        payloads[f"monophony/_vendor/unison/bin/{name}"] = data
    payloads["monophony/_vendor/source/unison.tar.gz"] = source
    if "watcher" in spec:
        payloads["monophony/_vendor/source/unison-fsmonitor.tar.gz"] = fetch(
            LOCK["watcher_source"], config.get("artifact-dir")
        )
    payloads["monophony/_vendor/unison-artifacts.json"] = (
        ROOT / "unison-artifacts.json"
    ).read_bytes()
    payloads["monophony/_vendor/LICENSE-Unison"] = (ROOT / "LICENSE").read_bytes()
    payloads["monophony/_vendor/NOTICE.md"] = (ROOT / "docs/redistribution.md").read_bytes()
    for path in (ROOT / "third_party").iterdir():
        if path.is_file():
            payloads[f"monophony/_vendor/licenses/{path.name}"] = path.read_bytes()
    with tempfile.TemporaryDirectory() as temp:
        filename = build_sdist(temp)
        payloads["monophony/_vendor/source/monophony.tar.gz"] = (Path(temp) / filename).read_bytes()
    filename = f"monophony-{VERSION}-py3-none-{spec['wheel_tag']}.whl"
    destination = Path(wheel_directory)
    destination.mkdir(parents=True, exist_ok=True)
    records = []
    # Write atomically: a failed build never leaves a partially valid wheel.
    with tempfile.TemporaryDirectory(dir=destination) as staging:
        archive_path = Path(staging) / filename
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:

            def write(name, data):
                info = zipfile.ZipInfo(name, (2020, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.compress_type = zipfile.ZIP_DEFLATED
                mode = 0o755 if name.startswith("monophony/_vendor/unison/bin/") else 0o644
                info.external_attr = (stat.S_IFREG | mode) << 16
                archive.writestr(info, data)

            for name, data in sorted(payloads.items()):
                write(name, data)
                digest = (
                    base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
                )
                records.append((name, "sha256=" + digest, str(len(data))))
            record = f"{DIST}/RECORD"
            records.append((record, "", ""))
            stream = io.StringIO(newline="")
            csv.writer(stream).writerows(records)
            write(record, stream.getvalue().encode())
        os.replace(archive_path, destination / filename)
    return filename
