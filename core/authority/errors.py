"""The one error the authority model raises."""

from __future__ import annotations


class AuthorityError(ValueError):
    """The policy file says something the model cannot hold."""
