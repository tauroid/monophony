import tempfile
import unittest
from pathlib import Path

from monophony.errors import TranslationError
from monophony.runner import parse_arguments


class ArgumentsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.a, self.b = root / "a", root / "b"
        self.a.mkdir()
        self.b.mkdir()

    def parse(self, *options):
        return parse_arguments([str(self.a), str(self.b), *options])

    def test_watch_default_and_disable(self):
        self.assertIn("watch", self.parse().options)
        once = self.parse("-watch=false")
        self.assertNotIn("-repeat", once.options)
        self.assertIn("-watch=false", once.options)
        self.assertIn("60", self.parse("-watch=false", "-repeat", "60").options)
        self.assertIn("", self.parse("-repeat", "").options)
        with self.assertRaises(TranslationError):
            self.parse("-watch=false", "-repeat", "watch")

    def test_safe_passthrough_and_fixed_settings(self):
        result = self.parse("-batch", "-log=false", "-retry=3", "-ignorecase", "false")
        self.assertIn("-log=false", result.options)
        self.assertIn("3", result.options)
        for args in [
            ("-ignore", "Name *"),
            ("-ignorenot", "Name *"),
            ("-follow", "Name *"),
            ("-path", "build/secret"),
            ("-source", "bad.prf"),
            ("-ignorecase", "true"),
            ("-unicode", "true"),
            ("-force", str(self.a)),
            ("-unknown",),
        ]:
            with self.subTest(args=args), self.assertRaises(TranslationError):
                self.parse(*args)

    def test_root_forms(self):
        result = parse_arguments(
            ["-root", str(self.a), "-root", "ssh://host//srv/project", "-batch"]
        )
        self.assertEqual(result.local_roots, (self.a,))
        for roots in [
            ["myprofile"],
            [str(self.a), str(self.a)],
            [str(self.a), str(self.a / "nested")],
            ["ssh://a/x", "ssh://b/x"],
        ]:
            with (
                self.subTest(roots=roots),
                self.assertRaises((TranslationError, FileNotFoundError)),
            ):
                parse_arguments(roots)


if __name__ == "__main__":
    unittest.main()
