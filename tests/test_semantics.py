import itertools
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from monophony import language as L
from monophony.errors import TranslationError
from monophony.gitignore import compile_policies, discover, parse, policy


@unittest.skipUnless(shutil.which("git"), "Git is needed as an independent oracle")
class GitSemanticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        self.env = os.environ | {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}

    def oracle(self, paths):
        result = subprocess.run(
            [
                "git",
                "-c",
                f"core.excludesFile={os.devnull}",
                "check-ignore",
                "--no-index",
                "-z",
                "--stdin",
            ],
            cwd=self.root,
            env=self.env,
            input="\0".join(paths).encode() + b"\0",
            check=False,
            capture_output=True,
        )
        self.assertIn(result.returncode, (0, 1), result.stderr)
        return set(result.stdout.decode().strip("\0").split("\0")) if result.stdout else set()

    def compare(self, text, paths):
        (self.root / ".gitignore").write_text(text)
        actual = policy(parse(text))
        expected = self.oracle(paths)
        for path in paths:
            self.assertEqual(actual.ignored(path), path in expected, (text, path))

    def test_order_parent_and_glob_semantics(self):
        patterns = [
            "*.log\n!important.log\nimportant.log\n",
            "build/\n!build/keep.txt\n",
            "build/*\n!build/keep.txt\n",
            "a/**/b\n",
            "**/b\n",
            "a/**\n",
            "a/*/b\n",
            "*\n!*/\n!*.py\n",
            "/a\n!a/keep.txt\n",
            "a\n!a\n",
            "*.log\n!a/*.log\na/b.log\n",
            "[ab]?.log\n",
            "[!a-c]*\n",
            "[^a-c]*\n",
            "\\#literal\n\\!literal\nspace\\ \ntrailing   \n",
            "a\\*\na\\?\na\\[\na\\b\n",
            "foo\\\n",
            "[]a]\n",
            "[-a]\n",
            "a -> b\n",
            "a{b,c}\n",
            "a**b\n",
            "**\n!a\n",
        ]
        names = [
            "a",
            "b",
            "c",
            "ab",
            "a.py",
            "a.log",
            "b.log",
            "important.log",
            "keep.txt",
            ".hidden",
            "é.log",
            "😀.py",
            "[",
            "]",
            "-",
            "a*",
            "a?",
            "a[",
            "#literal",
            "!literal",
            "space ",
            "trailing",
            "a -> b",
            "a{b,c}",
        ]
        paths = names + [
            prefix + "/" + name for prefix in ("a", "build", "x", "x/a", "a/x/y") for name in names
        ]
        for text in patterns:
            with self.subTest(pattern=text):
                self.compare(text, paths)

    def test_generated_orderings(self):
        paths = ["a", "b", "a.log", "b.log", "a/b", "a/a.log", "x/a/b", "x/a.log"]
        rules = ["a", "a/", "!a", "*.log", "!a.log", "a/*", "!a/b"]
        for combination in itertools.product(rules, repeat=2):
            self.compare("\n".join(combination), paths)

    def test_nested_scope_and_pruning(self):
        (self.root / ".gitignore").write_text("*.log\nblocked/\n")
        (self.root / "sub").mkdir()
        (self.root / "sub/.gitignore").write_text("!keep.log\n/local.txt\n")
        (self.root / "blocked").mkdir()
        (self.root / "blocked/.gitignore").write_text("!keep.log\nunsupported[\n")
        actual = discover(self.root)
        paths = [
            "root.log",
            "keep.log",
            "sub/keep.log",
            "sub/other.log",
            "sub/local.txt",
            "sub/deep/local.txt",
            "blocked/keep.log",
        ]
        expected = self.oracle(paths)
        for path in paths:
            self.assertEqual(actual.ignored(path), path in expected, path)
        self.assertFalse(any("blocked/.gitignore" in rule.origin for rule in actual.rules))

    def test_symlink_ignore_file_is_not_read(self):
        if not hasattr(os, "symlink"):
            self.skipTest("symlinks unavailable")
        (self.root / "rules").write_text("*\n")
        try:
            (self.root / ".gitignore").symlink_to("rules")
        except OSError:
            self.skipTest("symlinks not permitted")
        self.assertEqual(discover(self.root).rules, ())


class CompilerTests(unittest.TestCase):
    def test_directory_only_inclusion_rejected(self):
        with self.assertRaisesRegex(TranslationError, "Directory-only inclusion"):
            compile_policies([policy(parse("*\n!*/\n!*.py"))])

    def test_harmless_directory_only_inclusion_is_allowed(self):
        self.assertEqual(compile_policies([policy(parse("!*/"))]), [])

    def test_contradictions_have_symbolic_witnesses(self):
        with self.assertRaisesRegex(TranslationError, "important.log"):
            compile_policies(
                [policy(parse("*.log", source="A")), policy(parse("!important.log", source="B"))]
            )
        with self.assertRaisesRegex(TranslationError, "Contradictory"):
            compile_policies([policy(parse("build/")), policy(parse("!build/"))])

    def test_unspecified_does_not_conflict(self):
        self.assertTrue(compile_policies([policy(parse("*.log")), policy([])]))

    def test_unsupported_patterns_are_located(self):
        for text in ["[[:alpha:]]", "é", "[z-a]", "a/***/b", "[", "a//b"]:
            with self.subTest(text=text), self.assertRaisesRegex(TranslationError, "example:1"):
                parse(text, source="example")

    def test_set_algebra_witness_and_limits(self):
        a, b = L.literal("a"), L.literal("b")
        self.assertIsNone(L.witness(L.difference(L.union(a, b), L.union(b, a))))
        self.assertEqual(L.witness(L.difference(L.union(a, b), a)), "b")
        from unittest.mock import patch

        with patch.object(L, "MAX_STATES", 2), self.assertRaises(TranslationError):
            L.regex(L.literal("abcd"))

    def test_typed_directory_policy(self):
        result = policy(parse("build/"))
        self.assertFalse(result.ignored("build"))
        self.assertTrue(result.ignored("build", directory=True))
        self.assertTrue(result.ignored("build/file"))


if __name__ == "__main__":
    unittest.main()
