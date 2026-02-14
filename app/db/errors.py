"""Errors raised while validating or running SQL. Their messages are safe to show the model."""

from __future__ import annotations


class SqlError(Exception):
    """Base class for SQL problems that the model can read and correct."""


class SqlValidationError(SqlError):
    """The SQL was rejected before it reached the database."""


class SqlExecutionError(SqlError):
    """The database rejected or failed to run the SQL."""
