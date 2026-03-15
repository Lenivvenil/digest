"""Enables running the package as: python -m src"""
import asyncio
import sys

from src.main import main

sys.exit(asyncio.run(main()))
