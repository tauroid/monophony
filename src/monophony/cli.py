import sys

from .errors import TranslationError
from .runner import sync

HELP = """Usage: monophony ROOT ROOT [UNISON OPTIONS]
       monophony -root ROOT -root ROOT [UNISON OPTIONS]

Live synchronization using Unison's watcher and checked .gitignore translation.
Nested local .gitignores are read once at startup. Use -repeat '' for one run.
Only verified options are accepted; profiles and ignore overrides are blocked.

Examples:
  monophony ./project ssh://host//srv/project -batch
  monophony ./left ./right -batch -repeat ''

-version prints bundled Unison's version; -help prints Unison's option reference
(that reference includes options monophony deliberately blocks).
"""


def main():
    if sys.argv[1:] == ["--help"]:
        print(HELP)
        return 0
    try:
        result = sync(*sys.argv[1:])
        return result if result >= 0 else 128 - result
    except (TranslationError, OSError) as exc:
        print(f"monophony: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
