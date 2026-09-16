import itertools
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from monophony import language as L
from monophony.errors import TranslationError
from monophony.gitignore import compile_policies, discover, ignore_arguments, parse, policy


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
        lowered = L.lower(actual.files, valid_paths=True)
        expected = self.oracle(paths)
        for path in paths:
            self.assertEqual(actual.ignored(path), path in expected, (text, path))
            self.assertEqual(L.matches(lowered, path), path in expected, (text, path, "lowered"))

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

    def test_longer_ordered_rule_set(self):
        rules = "*.log\n!keep.log\nlogs/\n!logs/\nlogs/*.tmp\n!logs/keep.tmp\n"
        paths = [
            "a.log",
            "keep.log",
            "logs",
            "logs/a.log",
            "logs/a.tmp",
            "logs/keep.tmp",
            "other/a.tmp",
            "deep/keep.log",
        ]
        self.compare(rules, paths)

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
        with patch.object(L, "MAX_STATES", 2), self.assertRaises(TranslationError):
            L.compiled(L.literal("abcd"))

    def test_many_literal_rules_do_not_exhaust_automaton_states(self):
        rules = "".join(f"file{i}.log\n" for i in range(120))
        result = policy(parse(rules))
        self.assertTrue(result.ignored("file119.log"))
        self.assertFalse(result.ignored("file120.log"))
        self.assertTrue(compile_policies([result]))

    def test_distinct_literal_rules_compile(self):
        rules = "".join(f"{i:04x}{(i * 7919) % 65536:04x}.log\n" for i in range(40))
        self.assertTrue(compile_policies([policy(parse(rules))]))

    def test_ordinary_exclusions_need_no_automata(self):
        rules = "".join(f"{i:04x}{(i * 7919) % 65536:04x}.log\n" for i in range(80))
        with patch.object(L, "dfa", side_effect=AssertionError("Unexpected automaton")):
            self.assertTrue(compile_policies([policy(parse(rules)), policy(parse("*.tmp"))]))

    def test_many_nested_ignore_files_need_no_automata(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".gitignore").write_text(
                "*.log\n*.tmp\n*.pyc\n*.swp\n*.bak\n*.o\n"
                "node_modules/\n.venv/\nbuild/\ndist/\n.cache/\n.DS_Store\n"
            )
            for i in range(50):
                directory = root / f"packages/module-{i:02}/generated"
                (directory / "ignored-child").mkdir(parents=True)
                (directory / ".gitignore").write_text("*\n")
                (directory / "ignored-child/.gitignore").write_text("unsupported[\n")
            with patch.object(L, "dfa", side_effect=AssertionError("Unexpected automaton")):
                arguments = ignore_arguments([root])
            self.assertTrue(arguments)
            self.assertLess(sum(map(len, arguments)), 24_000)

    def test_scoped_negation_does_not_pull_in_unrelated_rules(self):
        rules = parse("*.log\n!keep.log", scope="special")
        for i in range(50):
            rules += parse("*", scope=f"packages/module-{i:02}/generated")
        L.lower.cache_clear()
        with patch.object(L, "MAX_STATES", 64), patch.object(L, "dfa", wraps=L.dfa) as automata:
            self.assertTrue(compile_policies([policy(rules)]))
            self.assertGreater(automata.call_count, 0)

    def test_disjoint_root_policies_need_no_intersection_automaton(self):
        a = policy(parse("*.log", scope="a"))
        b = policy(parse("!keep.log", scope="b"))
        with patch.object(L, "dfa", side_effect=AssertionError("Unexpected automaton")):
            self.assertTrue(compile_policies([a, b]))

    def test_typed_directory_policy(self):
        result = policy(parse("build/"))
        self.assertFalse(result.ignored("build"))
        self.assertTrue(result.ignored("build", directory=True))
        self.assertTrue(result.ignored("build/file"))


if __name__ == "__main__":
    unittest.main()
