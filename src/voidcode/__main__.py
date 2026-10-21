"""``python -m voidcode``: the core CLI is the product entry point."""

from .core.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
