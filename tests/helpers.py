from pathlib import Path

from alembic.config import Config
from sqlalchemy import Connection

ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"


def alembic_config(connection: Connection) -> Config:
    """Alembic config that migrates over `connection` instead of the settings URL."""
    config = Config(ALEMBIC_INI)
    config.attributes["connection"] = connection
    config.attributes["configure_logger"] = False
    return config


# Produced by the Webhook Relay's own PHP signer (Zasmall\RelaySignature\Signer::header) for two
# active secrets, proving the PHP signer and this verifier agree byte for byte.
GOLDEN_BODY = (
    b'{"id":"evt_golden_0001","type":"transaction.categorized","created_at":"2026-01-01T00:00:00Z",'
    b'"data":{"entity_id":"17","transaction":{"id":"48213","account_id":"3","posted_on":"2025-12-31",'
    b'"amount":"-129.99","currency":"USD","description":"ADOBE *CREATIVE CLD","vendor":"Adobe",'
    b'"category":"Software & Subscriptions"}}}'
)
GOLDEN_TIMESTAMP = 1767225600
GOLDEN_HEADER = (
    "t=1767225600,"
    "v1=5c0260e6e708530371eee5149744d195bdae421dfa303aa021589751490267f6,"
    "v1=ed80f9b2f94d6ee30741341f06c25ef8a65df0eeb13af94ecfacf55456f8c35e"
)
