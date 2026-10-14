"""Importing this package registers every check."""

from . import content, hosts, indexing, links, media, site  # noqa: F401
from .base import CHECKS, SEVERITIES, Check, Context, Finding, Item, Previous, run_checks

__all__ = ["CHECKS", "SEVERITIES", "Check", "Context", "Finding", "Item", "Previous", "run_checks"]
