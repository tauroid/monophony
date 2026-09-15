"""Real Unison transfers, with Git as an independent exclusion oracle."""

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from monophony.errors import TranslationError
from monophony.runner import executable

try:
    BINARY = str(executable())
except (TranslationError, OSError):
    BINARY = None


@unittest.skipUnless(BINARY, "Install monophony with uv sync to run Unison integration tests")
class UnisonIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.a, self.b = self.root / "a", self.root / "b"
        self.a.mkdir()
        self.b.mkdir()
        self.state = self.root / "state"
        self.state.mkdir()
        self.env = os.environ | {
            "MONOPHONY_UNISON": BINARY,
            "UNISON": str(self.state),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        }

    def put(self, path, value="contents"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)

    def command(self, *options):
        return [
            sys.executable,
            "-m",
            "monophony",
            str(self.a),
            str(self.b),
            "-batch",
            "-silent",
            *options,
        ]

    def sync(self, expected=(0,), *options):
        result = subprocess.run(
            self.command("-watch=false", *options),
            env=self.env,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertIn(result.returncode, expected, result.stdout + result.stderr)
        return result

    def test_order_nested_rules_remote_paths_and_default_profile_isolation(self):
        self.put(self.state / "default.prf", "ignore = Name *\n")
        self.put(
            self.a / ".gitignore", "*.log\n!important.log\nimportant.log\nbuild/\n!build/keep.txt\n"
        )
        self.put(self.a / "sub/.gitignore", "!keep.log\n")
        for name in [
            "normal",
            "bad.log",
            "important.log",
            "sub/keep.log",
            "sub/bad.log",
            "build/keep.txt",
        ]:
            self.put(self.a / name)
        self.put(self.b / "remote.log", "remote-only ignored content")
        self.sync()
        for name in ["normal", "sub/keep.log"]:
            self.assertTrue((self.b / name).is_file())
        for name in ["bad.log", "important.log", "sub/bad.log", "build/keep.txt"]:
            self.assertFalse((self.b / name).exists(), name)
        self.assertFalse((self.a / "remote.log").exists())
        self.assertEqual((self.b / "remote.log").read_text(), "remote-only ignored content")
        self.assertTrue((self.b / "build").is_dir())
        self.assertFalse(list(self.state.glob("monophony-*")))

    @unittest.skipUnless(shutil.which("git"), "Git oracle unavailable")
    def test_lowered_regex_against_git(self):
        names = [
            "a.log",
            "keep.log",
            "important.log",
            ".hidden",
            "a.py",
            "ab.txt",
            "z.txt",
            "#literal",
            "!literal",
            "a{b,c}",
            "a -> b",
            "space ",
            "é.log",
            "😀.py",
        ]
        if os.name == "nt":
            names.remove("space ")
            names.remove("a -> b")  # '>' is not a Windows filename character.
        paths = names + [p + "/" + n for p in ("sub", "build", "x/sub") for n in names]
        patterns = [
            "*.log\n!keep.log\nimportant.log\nbuild/\n!build/a.py\n",
            "**/a.py\nsub/*.txt\n!sub/ab.txt\n",
            "[!a-c]*.txt\n\\#literal\n\\!literal\na{b,c}\na -> b\nspace\\ \n",
        ]
        for index, text in enumerate(patterns):
            with self.subTest(pattern=text):
                a = self.root / f"source{index}"
                b = self.root / f"dest{index}"
                a.mkdir()
                b.mkdir()
                self.put(a / ".gitignore", text)
                for path in paths:
                    self.put(a / path)
                gitdir = self.root / f"git{index}"
                subprocess.run(["git", "init", "-q", str(gitdir)], check=True)
                oracle = subprocess.run(
                    [
                        "git",
                        "-c",
                        f"core.excludesFile={os.devnull}",
                        "check-ignore",
                        "--no-index",
                        "-z",
                        "--stdin",
                    ],
                    cwd=a,
                    env=self.env | {"GIT_DIR": str(gitdir / ".git"), "GIT_WORK_TREE": str(a)},
                    input=("\0".join(paths) + "\0").encode(),
                    check=False,
                    capture_output=True,
                )
                self.assertIn(oracle.returncode, (0, 1), oracle.stderr)
                ignored = set(oracle.stdout.decode().strip("\0").split("\0"))
                result = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "monophony",
                        str(a),
                        str(b),
                        "-batch",
                        "-silent",
                        "-watch=false",
                    ],
                    env=self.env,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                for path in paths:
                    self.assertEqual((b / path).is_file(), path not in ignored, path)

    def test_parent_deletion_also_deletes_ignored_children(self):
        self.put(self.a / ".gitignore", "build/\n")
        (self.a / "build").mkdir()
        self.sync()
        self.put(self.b / "build/secret", "ignored content")
        (self.a / "build").rmdir()
        self.sync()
        self.assertFalse((self.b / "build").exists())

    def test_parent_replacement_also_deletes_ignored_children(self):
        self.put(self.a / ".gitignore", "build/\n")
        (self.a / "build").mkdir()
        self.sync()
        self.put(self.b / "build/secret", "ignored content")
        (self.a / "build").rmdir()
        self.put(self.a / "build", "now a regular file")
        self.sync()
        self.assertEqual((self.b / "build").read_text(), "now a regular file")

    def test_directory_pattern_does_not_ignore_regular_file(self):
        self.put(self.a / ".gitignore", "build/\n")
        self.put(self.a / "build", "a file")
        self.sync()
        self.assertEqual((self.b / "build").read_text(), "a file")

    def test_directory_pattern_does_not_ignore_symlink(self):
        self.put(self.a / ".gitignore", "build/\n")
        self.put(self.a / "target/file", "target contents")
        try:
            (self.a / "build").symlink_to("target", target_is_directory=True)
        except OSError:
            self.skipTest("symlinks not permitted")
        self.sync()
        self.assertTrue((self.b / "build").is_symlink())
        self.assertEqual(os.readlink(self.b / "build"), "target")

    def test_type_conflict_is_reported(self):
        self.put(self.a / ".gitignore", "build/\n")
        self.put(self.a / "build/secret", "secret")
        self.put(self.b / "build", "other file")
        self.sync(expected=(1,))
        self.assertEqual((self.a / "build/secret").read_text(), "secret")
        self.assertEqual((self.b / "build").read_text(), "other file")

    def test_live_watcher_covers_future_files(self):
        self.put(self.a / ".gitignore", "*.log\nbuild/\n")
        self.put(self.a / "initial", "initial")
        with tempfile.TemporaryFile() as output:
            lifecycle = (
                {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
                if os.name == "nt"
                else {"start_new_session": True}
            )
            process = subprocess.Popen(
                self.command(), env=self.env, stdout=output, stderr=output, **lifecycle
            )
            try:

                def wait_for(path):
                    deadline = time.monotonic() + 15
                    while time.monotonic() < deadline:
                        if path.exists():
                            return
                        if process.poll() is not None:
                            break
                        time.sleep(0.1)
                    output.seek(0)
                    self.fail(f"Watcher did not sync {path}: {output.read().decode()}")

                wait_for(self.b / "initial")
                self.put(self.a / "new.log", "ignored")
                self.put(self.a / "build/new", "ignored")
                self.put(self.a / "new.txt", "included")
                wait_for(self.b / "new.txt")
                self.assertFalse((self.b / "new.log").exists())
                self.assertFalse((self.b / "build/new").exists())
            finally:
                if process.poll() is None:
                    if os.name == "nt":
                        subprocess.run(
                            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                            check=True,
                            capture_output=True,
                        )
                        process.wait(timeout=10)
                    else:
                        os.killpg(process.pid, signal.SIGINT)
                        try:
                            process.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            os.killpg(process.pid, signal.SIGKILL)
                            process.wait()


if __name__ == "__main__":
    unittest.main()
