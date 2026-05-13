#!/usr/bin/env python3
"""Convenience launcher.

Run with::

    .venv/bin/python run.py

equivalent to ``python -m safedrop``.
"""

from safedrop.gui import main

if __name__ == "__main__":
    main()
