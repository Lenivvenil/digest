"""Enables running the package as: python -m digest"""
import asyncio
import sys

from digest.main import main

sys.exit(asyncio.run(main()))
