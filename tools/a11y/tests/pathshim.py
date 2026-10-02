"""Puts tools/a11y/ and tools/ on sys.path so tests import stage modules and mapdata directly (KTD27)."""
import os
import sys

A11Y = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.dirname(A11Y)
ROOT = os.path.dirname(TOOLS)
for p in (A11Y, TOOLS):
    if p not in sys.path:
        sys.path.insert(0, p)
