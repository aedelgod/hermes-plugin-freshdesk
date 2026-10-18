"""Hermes plugin entry point."""

from .freshdesk_connector import register

__all__ = ["register"]
