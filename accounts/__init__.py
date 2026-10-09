"""Store accounts: sign-in by emailed link, a separate account per store,
the store's saved master and its run history.

Off unless DATABASE_URL is set. See accounts/web.py for the pages and
accounts/schema.sql for what is stored.
"""
from .db import enabled  # noqa: F401
from .web import init_app  # noqa: F401
