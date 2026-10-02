"""Carrier adapters. Importing this package registers every adapter with the registry.

To add a carrier, import its adapter module here (see app/carriers/_template/adapter.py).
"""

from app.carriers.ekart import adapter as _ekart  # noqa: F401
from app.carriers.mock import adapter as _mock  # noqa: F401
from app.carriers.xpressbees import adapter as _xpressbees  # noqa: F401
