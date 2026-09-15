import copy
import json
import struct
import unittest
from unittest.mock import patch

from tools import update_binaries as updater


class UpdateTests(unittest.TestCase):
    def test_missing_hash_and_ambiguous_asset_are_rejected(self):
        release = {
            "assets": [
                {"name": "binary.tar.gz", "browser_download_url": "https://example.org/binary"}
            ]
        }
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            updater.asset(release, r"binary\.tar\.gz")
        release["assets"] *= 2
        with self.assertRaisesRegex(ValueError, "found 2"):
            updater.asset(release, r"binary\.tar\.gz")

    def test_macos_version_and_architecture(self):
        data = bytearray(56)
        data[:4] = b"\xcf\xfa\xed\xfe"
        struct.pack_into("<I", data, 4, 0x100000C)
        struct.pack_into("<I", data, 16, 1)
        struct.pack_into("<IIII", data, 32, 0x32, 24, 1, (13 << 16) | (2 << 8))
        self.assertEqual(updater.macos_minimum(data, "arm64"), (13, 2))
        with self.assertRaisesRegex(ValueError, "architecture"):
            updater.macos_minimum(data, "x86_64")

    def test_changed_source_of_existing_release_fails_before_writing(self):
        path = updater.ROOT / "unison-artifacts.json"
        before = path.read_bytes()
        manifest = json.loads(before)
        changed = copy.deepcopy(manifest["source"])
        changed["sha256"] = "0" * 64
        releases = [
            {"tag_name": "v" + manifest["version"]},
            {"tag_name": "v" + manifest["watcher_version"]},
        ]
        with (
            patch.object(updater, "release", side_effect=releases),
            patch.object(
                updater,
                "source",
                side_effect=[(changed, b"changed"), (manifest["watcher_source"], b"watcher")],
            ),
            patch.object(updater, "fetch") as fetch,
            self.assertRaisesRegex(ValueError, "changed source bytes"),
        ):
            updater.update(manifest["version"], manifest["watcher_version"])
        fetch.assert_not_called()
        self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
