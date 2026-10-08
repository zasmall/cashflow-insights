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
