"""MCP tools, called over the real protocol: in-process, plus one stdio smoke test."""

import os
import sys
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from typing import Any

import pytest
from mcp import Client, StdioServerParameters
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from cashflow.db.session import make_read_only_session_factory
from cashflow.demo.seed import seed
from cashflow.mcp_server.server import create_server
from cashflow.settings import Settings

pytestmark = [pytest.mark.db, pytest.mark.anyio]

AS_OF = date(2026, 10, 8)
NOW = datetime(2026, 10, 8, 15, tzinfo=UTC)
SETTINGS = Settings(_env_file=None)
TOOLS = {
    "list_entities",
    "get_weekly_summary",
    "get_forecast",
    "list_anomalies",
    "explain_anomaly",
    "spend_by_category",
    "list_recurring",
}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def client(session: Session, session_factory: sessionmaker[Session]) -> AsyncIterator[Client]:
    seed(session, entities=2, base_seed=1, as_of=AS_OF, settings=SETTINGS)
    session.commit()
    server = create_server(SETTINGS, session_factory=session_factory, clock=lambda: NOW)
    async with Client(server) as client:
        yield client


async def call(client: Client, tool: str, **arguments: Any) -> Any:
    result = await client.call_tool(tool, arguments)
    assert not result.is_error, result.content
    return result.structured_content


async def error(client: Client, tool: str, **arguments: Any) -> str:
    result = await client.call_tool(tool, arguments)
    assert result.is_error
    text_content = result.content[0]
    return str(getattr(text_content, "text", ""))


# Guards ----------------------------------------------------------------------------------------


async def test_exactly_the_allowed_tools_exist_and_all_are_read_only(client: Client) -> None:
    """Adding a tool, especially one that writes, should take a deliberate change here."""
    tools = (await client.list_tools()).tools

    assert {t.name for t in tools} == TOOLS
    for tool in tools:
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is True, tool.name
        assert tool.annotations.destructive_hint is False, tool.name
        assert tool.output_schema is not None, f"{tool.name} returns a typed model"
        assert tool.description, tool.name


def test_server_sessions_cannot_write(engine: Engine) -> None:
    factory = make_read_only_session_factory(engine)

    with factory() as session, pytest.raises(DBAPIError, match="read-only transaction"):
        session.execute(text("CREATE TABLE should_not_exist (id int)"))


# Tools -----------------------------------------------------------------------------------------


async def test_list_entities(client: Client) -> None:
    entities = (await call(client, "list_entities"))["result"]

    assert [e["id"] for e in entities] == ["demo-1", "demo-2"]
    assert all(e["open_anomalies"] == 5 for e in entities)
    assert all(e["last_transaction_on"] == AS_OF.isoformat() for e in entities)


async def test_get_weekly_summary(client: Client) -> None:
    summary = await call(client, "get_weekly_summary", entity_id="demo-1")

    assert summary["week_start"] == "2026-09-28"
    assert summary["outlook"]["lowest_expected"]["balance"]


async def test_get_forecast(client: Client) -> None:
    forecast = await call(client, "get_forecast", entity_id="demo-1", horizon_days=60)

    assert len(forecast["points"]) == 60
    assert forecast["backtest"]["balance_error"] is not None


async def test_list_and_explain_anomalies(client: Client) -> None:
    anomalies = (await call(client, "list_anomalies", entity_id="demo-1"))["result"]
    duplicate = next(a for a in anomalies if a["type"] == "duplicate_charge")

    detail = await call(client, "explain_anomaly", entity_id="demo-1", anomaly_id=duplicate["id"])

    assert detail["anomaly"]["id"] == duplicate["id"]
    evidence = detail["evidence"]
    assert [e["id"] for e in evidence] == sorted(duplicate["transaction_ids"])
    assert len({(e["vendor"], e["amount"]) for e in evidence}) == 1, "same vendor and amount"


async def test_spend_by_category(client: Client) -> None:
    report = await call(
        client, "spend_by_category", entity_id="demo-1", start="2026-09-01", end="2026-09-30"
    )

    outs = [c["money_out"] for c in report["categories"]]
    assert [float(o) for o in outs] == sorted((float(o) for o in outs), reverse=True)
    assert float(report["money_out"]) == pytest.approx(sum(float(o) for o in outs))


async def test_list_recurring(client: Client) -> None:
    series = (await call(client, "list_recurring", entity_id="demo-1"))["result"]

    assert {"Adobe", "Gusto Payroll", "Parkside Properties"} <= {s["vendor"] for s in series}
    adobe = next(s for s in series if s["vendor"] == "Adobe")
    assert (adobe["typical_amount"], adobe["projected_amount"]) == ("-89.99", "-104.99")
    dates = [s["next_expected_on"] for s in series]
    assert dates == sorted(dates)


# Scoping and bad input -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("get_weekly_summary", {}),
        ("get_forecast", {}),
        ("list_anomalies", {}),
        ("explain_anomaly", {"anomaly_id": 1}),
        ("spend_by_category", {"start": "2026-09-01", "end": "2026-09-30"}),
        ("list_recurring", {}),
    ],
)
async def test_unknown_entity_is_an_error(
    client: Client, tool: str, arguments: dict[str, Any]
) -> None:
    assert "entity not found" in await error(client, tool, entity_id="nobody", **arguments)


async def test_another_entitys_anomaly_is_not_found(client: Client) -> None:
    theirs = (await call(client, "list_anomalies", entity_id="demo-2"))["result"][0]

    message = await error(client, "explain_anomaly", entity_id="demo-1", anomaly_id=theirs["id"])

    assert "anomaly not found" in message


@pytest.mark.parametrize(
    ("tool", "arguments", "message"),
    [
        ("get_forecast", {"horizon_days": 45}, "horizon must be one of 30, 60, 90"),
        (
            "spend_by_category",
            {"start": "2026-09-30", "end": "2026-09-01"},
            "start must be on or before end",
        ),
        ("get_weekly_summary", {"week_of": "2026-10-12"}, "can't be in the future"),
        ("list_anomalies", {"status": "deleted"}, "status"),
    ],
)
async def test_bad_arguments_are_errors(
    client: Client, tool: str, arguments: dict[str, Any], message: str
) -> None:
    assert message in await error(client, tool, entity_id="demo-1", **arguments)


# The real entry point --------------------------------------------------------------------------


async def test_stdio_entry_point_serves_the_tools(engine: Engine) -> None:
    env = {**os.environ, "DATABASE_URL": engine.url.render_as_string(hide_password=False)}
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "cashflow.mcp_server"], env=env
    )

    async with Client(params) as client:
        tools = (await client.list_tools()).tools
        result = await client.call_tool("list_entities", {})

    assert {t.name for t in tools} == TOOLS
    assert not result.is_error
