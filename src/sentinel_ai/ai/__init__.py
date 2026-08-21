"""Shared model-server plumbing. The reviewer that uses it lives in `diff_review`."""

from .client import AIUnavailable, health_check

__all__ = ["AIUnavailable", "health_check"]
