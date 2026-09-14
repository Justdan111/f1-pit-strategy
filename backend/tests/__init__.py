"""Marks `tests` as a package so helpers can be imported from conftest.

Without this, `from .conftest import ...` fails: pytest imports test modules
as top-level by default, leaving them with no parent package.
"""
