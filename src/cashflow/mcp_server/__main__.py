"""`python -m cashflow.mcp_server`: serve the MCP tools over stdio."""

import logging
import sys

from cashflow.mcp_server.server import create_server
from cashflow.settings import get_settings


def main() -> None:
    # stdout carries the protocol over stdio, so logs must go to stderr.
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    create_server(get_settings()).run("stdio")


if __name__ == "__main__":
    main()
