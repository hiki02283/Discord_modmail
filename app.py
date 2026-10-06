#!/usr/bin/env python3
"""HeavenCloud / Pterodactyl entry point for ModMail.

Starts the existing src/modmail application without duplicating any logic.
Requires Python 3.13+ at deployment time; the code itself depends only on
standard-library patterns plus discord.py, SQLAlchemy and pydantic-settings.
"""
import sys
from pathlib import Path

# Make the src/ package importable without editing the environment.
SRC = Path(__file__).resolve().parent / "src"
sys.path.insert(0, str(SRC))

from modmail.__main__ import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
