import os
import sys

if getattr(sys, "frozen", False):
    os.environ["PYDANTIC_DISABLE_PLUGINS"] = "1"

from getgather.cli import main

if __name__ == "__main__":
    main()
