"""Allow ``python -m chrome_on_device_infer``."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
