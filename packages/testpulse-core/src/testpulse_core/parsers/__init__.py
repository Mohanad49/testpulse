"""Format parsers.

Importing this package registers every built-in parser, so ``available_formats()``
is populated by the time anything asks. Registration is a module import side
effect, which is why these names are imported here and never used directly.
"""

from testpulse_core.parsers import allure, junit, playwright_json, pytest_json
from testpulse_core.parsers.base import (
    ParseError,
    Parser,
    UnknownFormatError,
    available_formats,
    get_parser,
    register,
)

__all__ = [
    "ParseError",
    "Parser",
    "UnknownFormatError",
    "allure",
    "available_formats",
    "get_parser",
    "junit",
    "playwright_json",
    "pytest_json",
    "register",
]
