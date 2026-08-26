"""Permet `python -m reviews <command>`."""

import sys

from reviews.cli import main

if __name__ == "__main__":
    sys.exit(main())
