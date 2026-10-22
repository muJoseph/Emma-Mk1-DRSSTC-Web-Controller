"""
Logging package for Python.
"""

# Re-export so callers can use "from logging_interface import Logger".
from .logger import Logger, TerminalLogger

__all__ = ['Logger', 'TerminalLogger'] 