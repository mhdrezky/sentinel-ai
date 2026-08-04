"""On-prem model integration."""

from .client import AIClient, AIUnavailable, parse_verdict

__all__ = ["AIClient", "AIUnavailable", "parse_verdict"]
