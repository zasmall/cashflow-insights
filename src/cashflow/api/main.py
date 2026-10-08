"""Entry point for `fastapi dev src/cashflow/api/main.py` and production ASGI servers."""

from cashflow.api.app import create_app
from cashflow.settings import get_settings

app = create_app(get_settings())
