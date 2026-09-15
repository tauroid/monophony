"""Refresh binary/source pins and generated notices together.

uv run python -m tools.update_binaries
uv run python -m tools.update_binaries --unison VERSION --watcher VERSION
"""

import argparse
import copy
import hashlib
import io
import json
import os
import re
import struct
import tarfile
from pathlib import Path
from urllib.request import Request, urlopen

from monophony_build import MAX_DOWNLOAD, binaries, check_static_linux, fetch
from tools.watcher_licenses import notices

ROOT = Path(__file__).resolve().parents[1]


def download(url):
    headers = {"User-Agent": "monophony-release-updater"}
    token = os.environ.get("GH_TOKEN")
    if token and url.startswith("https://api.github.com/"):
        headers["Authorization"] = "Bearer " + token
    with urlopen(Request(url, headers=headers), timeout=60) as response:
        data = response.read(MAX_DOWNLOAD + 1)
    if len(data) > MAX_DOWNLOAD:
        raise ValueError(f"Download too large: {url}")
    return data


def release(repo, version):
    suffix = "latest" if version == "latest" else "tags/v" + version.removeprefix("v")
    result = json.loads(download(f"https://api.github.com/repos/{repo}/releases/{suffix}"))
    if result.get("draft") or result.get("prerelease"):
        raise ValueError("Only published stable releases are supported")
    if not re.fullmatch(r"v?\d+\.\d+\.\d+", result["tag_name"]):
        raise ValueError(f"Unexpected release tag: {result['tag_name']}")
    return result


def asset(release, pattern):
    candidates = [a for a in release["assets"] if re.fullmatch(pattern, a["name"])]
    if len(candidates) != 1:
        raise ValueError(f"Expected one asset matching {pattern!r}, found {len(candidates)}")
    selected = candidates[0]
    digest = selected.get("digest") or ""
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError(f"Missing upstream SHA-256 digest: {selected['name']}")
    return {"url": selected["browser_download_url"], "sha256": digest.split(":")[1]}


def source(repo, tag):
    url = f"https://codeload.github.com/{repo}/tar.gz/refs/tags/{tag}"
    data = download(url)
    return {"url": url, "sha256": hashlib.sha256(data).hexdigest()}, data


def member(data, suffix):
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        matches = [m for m in archive if m.isfile() and m.name.endswith("/" + suffix)]
        if len(matches) != 1:
            raise ValueError(f"Expected one source file {suffix!r}")
        return archive.extractfile(matches[0]).read()


def macos_minimum(data, architecture):
    if data[:4] != b"\xcf\xfa\xed\xfe":
        raise ValueError("Expected a little-endian 64-bit Mach-O executable")
    expected_cpu = {"arm64": 0x100000C, "x86_64": 0x1000007}[architecture]
    if struct.unpack_from("<I", data, 4)[0] != expected_cpu:
        raise ValueError("Wrong Mach-O architecture")
    offset = 32
    minimum = None
    for _ in range(struct.unpack_from("<I", data, 16)[0]):
        command, size = struct.unpack_from("<II", data, offset)
        if size < 8 or offset + size > len(data):
            raise ValueError("Invalid Mach-O load command")
        if command == 0x32:  # LC_BUILD_VERSION
            platform, version = struct.unpack_from("<II", data, offset + 8)
            if platform != 1:
                raise ValueError("Expected macOS build platform")
            minimum = version
        elif command == 0x24:  # LC_VERSION_MIN_MACOSX
            minimum = struct.unpack_from("<I", data, offset + 8)[0]
        offset += size
    if minimum is None:
        raise ValueError("Missing macOS deployment target")
    return minimum >> 16, (minimum >> 8) & 255


def unchanged_release(old, new, key, version_key):
    if old[version_key] == new[version_key] and old[key] != new[key]:
        raise ValueError(
            f"Existing {version_key} has changed source bytes; refusing to silently repin"
        )


def update(unison, watcher, *, artifact_dir=None, check=False):
    current = json.loads((ROOT / "unison-artifacts.json").read_text())
    result = copy.deepcopy(current)
    u = release("bcpierce00/unison", unison)
    w = release("autozimu/unison-fsmonitor", watcher)
    result["version"] = u["tag_name"].removeprefix("v")
    result["watcher_version"] = w["tag_name"].removeprefix("v")
    result["source"], unison_source = source("bcpierce00/unison", u["tag_name"])
    result["watcher_source"], watcher_source = source("autozimu/unison-fsmonitor", w["tag_name"])
    unchanged_release(current, result, "source", "version")
    unchanged_release(current, result, "watcher_source", "watcher_version")
    # A changed upstream license needs review, not silently relabeling our package.
    if member(unison_source, "LICENSE") != (ROOT / "LICENSE").read_bytes():
        raise ValueError("Unison's license text has changed; review redistribution before updating")
    v = re.escape(result["version"])
    patterns = {
        "linux_x86_64": rf"unison-{v}-ubuntu-[0-9.]+-x86_64-static\.tar\.gz",
        "windows_x86_64": rf"unison-{v}-windows-x86_64\.zip",
        "macos_arm64": rf"unison-{v}-macos-arm64\.tar\.gz",
        "macos_x86_64": rf"unison-{v}-macos-x86-64\.tar\.gz",
    }
    cached = {
        result["source"]["sha256"]: unison_source,
        result["watcher_source"]["sha256"]: watcher_source,
    }
    rust_commits = set()
    for target, pattern in patterns.items():
        spec = asset(u, pattern)
        prior = current["targets"][target]
        if current["version"] == result["version"] and spec["sha256"] != prior["sha256"]:
            raise ValueError(f"Existing Unison release changed {target} bytes")
        payload = fetch(spec)
        cached[spec["sha256"]] = payload
        programs = binaries(
            payload,
            windows=target.startswith("windows"),
            wanted={"unison"} if target.startswith("macos") else None,
        )
        spec["wheel_tag"] = prior["wheel_tag"]
        if target == "linux_x86_64":
            check_static_linux(programs)
        if target.startswith("macos"):
            architecture = target.removeprefix("macos_")
            suffix = "aarch64" if architecture == "arm64" else "amd64"
            spec["watcher"] = asset(w, rf"unison-fsmonitor-macos-{suffix}\.tar\.gz")
            if (
                current["watcher_version"] == result["watcher_version"]
                and spec["watcher"]["sha256"] != prior["watcher"]["sha256"]
            ):
                raise ValueError(f"Existing watcher release changed {target} bytes")
            payload = fetch(spec["watcher"])
            cached[spec["watcher"]["sha256"]] = payload
            monitor = binaries(payload, wanted={"unison-fsmonitor"})["unison-fsmonitor"]
            major, minor = max(
                macos_minimum(programs["unison"], architecture),
                macos_minimum(monitor, architecture),
            )
            spec["wheel_tag"] = f"macosx_{major}_{minor}_{architecture}"
            rust_commits.update(x.decode() for x in re.findall(rb"/rustc/([0-9a-f]{40})", monitor))
        result["targets"][target] = spec
    generated = {
        "unison-fsmonitor-LICENSE": member(watcher_source, "LICENSE.txt"),
        "watcher-dependency-notices.txt": notices(
            watcher_source, result["watcher_version"]
        ).encode(),
    }
    if len(rust_commits) == 1:
        rust_commit = rust_commits.pop()
        for name in ("COPYRIGHT", "LICENSE-MIT", "LICENSE-APACHE"):
            url = f"https://raw.githubusercontent.com/rust-lang/rust/{rust_commit}/{name}"
            generated["Rust-" + name] = download(url)
        result["watcher_rust_revision"] = rust_commit
    else:
        result.pop("watcher_rust_revision", None)
        print("Rust runtime revision unavailable; existing runtime notices retained for review")
    print(f"Unison: {current['version']} -> {result['version']}")
    print(f"macOS watcher: {current['watcher_version']} -> {result['watcher_version']}")
    for name, payload in generated.items():
        previous = ROOT / "third_party" / name
        if not previous.exists() or previous.read_bytes() != payload:
            print(f"Notice added or changed; review: third_party/{name}")
    if check:
        print("Checked all selected archives and regenerated notices in memory; no files changed")
        return result
    # All network, checksum, format and notice checks complete before mutation.
    if artifact_dir:
        directory = Path(artifact_dir)
        directory.mkdir(parents=True, exist_ok=True)
        for digest, payload in cached.items():
            (directory / digest).write_bytes(payload)
    for name, payload in generated.items():
        (ROOT / "third_party" / name).write_bytes(payload)
    (ROOT / "unison-artifacts.json").write_text(json.dumps(result, indent=2) + "\n")
    print("Updated manifest and notices. Run uv sync, uv run pytest, and uv build.")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--unison", help="Release version, or latest")
    parser.add_argument("--watcher", help="Release version, or latest")
    parser.add_argument(
        "--artifact-dir", type=Path, help="Also save verified archives for offline builds"
    )
    parser.add_argument("--check", action="store_true", help="Verify without changing any files")
    args = parser.parse_args()
    current = json.loads((ROOT / "unison-artifacts.json").read_text())
    explicit = args.unison is not None or args.watcher is not None
    update(
        args.unison or (current["version"] if explicit else "latest"),
        args.watcher or (current["watcher_version"] if explicit else "latest"),
        artifact_dir=args.artifact_dir,
        check=args.check,
    )


if __name__ == "__main__":
    main()
