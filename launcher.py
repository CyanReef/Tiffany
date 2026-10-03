"""Standard-library entry point; no third-party imports before bootstrap."""
import sys

if sys.version_info < (3, 11):
    print("Tiffany requires Python 3.11+ and venv", file=sys.stderr)
    raise SystemExit(2)

from deployment.launcher import main

if __name__ == "__main__":
    raise SystemExit(main())
