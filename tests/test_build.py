import base64
import csv
import hashlib
import io
import json
import os
import struct
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import monophony_build as build


class BuildTests(unittest.TestCase):
    def test_linux_tag_requires_static_x86_64_binaries(self):
        data = bytearray(120)
        data[:7] = b"\x7fELF\x02\x01\x01"
        struct.pack_into("<HH", data, 16, 2, 62)
        struct.pack_into("<Q", data, 32, 64)
        struct.pack_into("<HH", data, 54, 56, 1)
        struct.pack_into("<I", data, 64, 1)
        build.check_static_linux({"unison": data})
        for kind in (2, 3):
            with self.subTest(program_header=kind):
                dynamic = data + bytearray(56)
                struct.pack_into("<H", dynamic, 56, 2)
                struct.pack_into("<I", dynamic, 120, kind)
                with self.assertRaisesRegex(ValueError, "statically linked"):
                    build.check_static_linux({"unison": dynamic})
        struct.pack_into("<H", data, 18, 183)  # AArch64
        with self.assertRaisesRegex(ValueError, "x86-64"):
            build.check_static_linux({"unison": data})
        with self.assertRaises(ValueError):
            build.check_static_linux({"unison": data[:80]})

    def test_checksum_failure_and_offline_success(self):
        payload = b"pinned bytes"
        digest = hashlib.sha256(payload).hexdigest()
        spec = {"sha256": digest, "url": "https://example.invalid/artifact"}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / digest
            path.write_bytes(payload)
            self.assertEqual(build.fetch(spec, directory), payload)
            path.write_bytes(b"tampered bytes")
            with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
                build.fetch(spec, directory)

    def test_rejects_unsafe_and_duplicate_archive_members(self):
        for names in [
            ["../unison", "bin/unison-fsmonitor"],
            ["bin/unison", "other/unison", "bin/unison-fsmonitor"],
        ]:
            stream = io.BytesIO()
            with tarfile.open(fileobj=stream, mode="w:gz") as archive:
                for name in names:
                    info = tarfile.TarInfo(name)
                    info.size = 1
                    archive.addfile(info, io.BytesIO(b"x"))
            with self.assertRaises(ValueError):
                build.binaries(stream.getvalue())

    def test_archive_never_follows_symlinks(self):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w:gz") as archive:
            info = tarfile.TarInfo("bin/unison")
            info.type, info.linkname = tarfile.SYMTYPE, "/bin/sh"
            archive.addfile(info)
        with self.assertRaises(ValueError):
            build.binaries(stream.getvalue())

    def test_only_cli_and_watcher_are_selected(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            for name in [
                "bin/unison.exe",
                "bin/unison-fsmonitor.exe",
                "bin/unison-gtk.exe",
                "bin/gui.dll",
            ]:
                archive.writestr(name, name.encode())
        self.assertEqual(
            set(build.binaries(stream.getvalue(), windows=True)),
            {"unison.exe", "unison-fsmonitor.exe"},
        )

    def test_explicit_cross_target_and_unknown_version(self):
        with patch.object(build.platform, "machine", return_value="unsupported"):
            self.assertEqual(build.settings({"target": "windows_x86_64"})[1], "windows_x86_64")
            with self.assertRaises(ValueError):
                build.settings()
        with self.assertRaises(ValueError):
            build.settings({"unison-version": "0.0.0"})
        with self.assertRaises(ValueError):
            build.settings({"target": "linux_arm64"})

    def test_sdist_contains_builder_and_licenses(self):
        with tempfile.TemporaryDirectory() as directory:
            filename = build.build_sdist(directory)
            with tarfile.open(Path(directory) / filename) as archive:
                names = archive.getnames()
            for name in [
                "monophony_build.py",
                "unison-artifacts.json",
                "pyproject.toml",
                "LICENSE",
                "src/monophony/language.py",
                "docs/redistribution.md",
            ]:
                self.assertIn(f"monophony-{build.VERSION}/{name}", names)
            self.assertFalse(any("__pycache__" in name or "_vendor" in name for name in names))

    @unittest.skipUnless(
        os.environ.get("MONOPHONY_ARTIFACT_DIR"), "Set MONOPHONY_ARTIFACT_DIR for real wheel builds"
    )
    def test_cross_wheels_records_sources_and_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            for target, spec in build.LOCK["targets"].items():
                with self.subTest(target=target):
                    filename = build.build_wheel(
                        directory,
                        {"target": target, "artifact-dir": os.environ["MONOPHONY_ARTIFACT_DIR"]},
                    )
                    self.assertTrue(filename.endswith(f"py3-none-{spec['wheel_tag']}.whl"))
                    with zipfile.ZipFile(Path(directory) / filename) as archive:
                        records = csv.reader(
                            io.StringIO(archive.read(f"{build.DIST}/RECORD").decode())
                        )
                        for name, digest, size in records:
                            if digest:
                                payload = archive.read(name)
                                self.assertEqual(len(payload), int(size))
                                actual = (
                                    base64.urlsafe_b64encode(hashlib.sha256(payload).digest())
                                    .rstrip(b"=")
                                    .decode()
                                )
                                self.assertEqual(digest, "sha256=" + actual)
                        names = archive.namelist()
                        self.assertIn("monophony/_vendor/source/unison.tar.gz", names)
                        self.assertIn("monophony/_vendor/source/monophony.tar.gz", names)
                        self.assertFalse(
                            any("gtk" in name or name.endswith(".dll") for name in names)
                        )
                        binary = "unison.exe" if target.startswith("windows") else "unison"
                        info = archive.getinfo("monophony/_vendor/unison/bin/" + binary)
                        self.assertEqual((info.external_attr >> 16) & 0o777, 0o755)
                        manifest = json.loads(
                            archive.read("monophony/_vendor/unison-artifacts.json")
                        )
                        self.assertEqual(manifest, build.LOCK)


if __name__ == "__main__":
    unittest.main()
