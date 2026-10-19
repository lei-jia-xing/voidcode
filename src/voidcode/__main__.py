"""``python -m voidcode``: the kernel CLI is the product entry point."""

from .kernel.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
