"""MCP server: read-only, entity-scoped tools over the core read services.

    uv run python -m cashflow.mcp_server        # stdio, for Claude Code and Claude Desktop

Every tool is annotated read-only, and the server's database sessions run in READ ONLY
transactions, so even a buggy tool can't write. Tools never take a write path.
"""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Annotated, Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field
from sqlalchemy.orm import Session, sessionmaker

from cashflow.core import queries
from cashflow.core.enums import AnomalyStatus
from cashflow.core.errors import InvalidRequestError, NotFoundError
from cashflow.core.queries import (
    AnomalyDetail,
    AnomalyOut,
    EntityOverview,
    ForecastOut,
    RecurringOut,
    SpendByCategory,
)
from cashflow.core.summary import WeeklySummary
from cashflow.db.session import make_engine, make_read_only_session_factory
from cashflow.settings import Settings

Clock = Callable[[], datetime]

INSTRUCTIONS = """\
Cash-flow insight for small businesses ("entities"). Start with list_entities to find an
entity id; every other tool is scoped to one entity. Money is a decimal string in the entity's
currency, and negative amounts are money out. get_weekly_summary is the best first call for
"how is this business doing?". All tools are read-only."""

READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)

EntityId = Annotated[str, Field(description="Entity id, from list_entities.")]


@dataclass(frozen=True)
class ServerContext:
    settings: Settings
    session_factory: sessionmaker[Session]
    clock: Clock


# The second parameter is the transport's request type, which tools never touch.
ToolContext = Context[ServerContext, Any]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def create_server(
    settings: Settings,
    *,
    session_factory: sessionmaker[Session] | None = None,
    clock: Clock = _utc_now,
) -> MCPServer[ServerContext]:
    """Build the server. Pass `session_factory` in tests; otherwise a read-only one is made."""

    @asynccontextmanager
    async def lifespan(_: MCPServer[ServerContext]) -> AsyncIterator[ServerContext]:
        if session_factory is not None:
            yield ServerContext(settings, session_factory, clock)
            return
        engine = make_engine(settings)
        try:
            yield ServerContext(settings, make_read_only_session_factory(engine), clock)
        finally:
            engine.dispose()

    server = MCPServer[ServerContext](
        name="cashflow-insights", instructions=INSTRUCTIONS, lifespan=lifespan
    )
    _register_tools(server)
    return server


def _query[T](ctx: ToolContext, fn: Callable[[Session, ServerContext], T]) -> T:
    """Run a read service in its own session, turning domain errors into tool errors."""
    state = ctx.request_context.lifespan_context
    with state.session_factory() as session:
        try:
            return fn(session, state)
        except (NotFoundError, InvalidRequestError) as exc:
            raise ToolError(str(exc)) from exc


def _register_tools(server: MCPServer[ServerContext]) -> None:
    @server.tool(annotations=READ_ONLY)
    def list_entities(ctx: ToolContext) -> list[EntityOverview]:
        """List the businesses available, with each one's currency, latest transaction date,
        open anomaly count, and whether new transactions are waiting for a refresh."""
        return _query(ctx, lambda session, _: queries.list_entities(session))

    @server.tool(annotations=READ_ONLY)
    def get_weekly_summary(
        entity_id: EntityId,
        ctx: ToolContext,
        week_of: Annotated[
            date | None,
            Field(description="Any date in the week to summarize. Default: last complete week."),
        ] = None,
    ) -> WeeklySummary:
        """Monday-Sunday summary: cash in and out vs recent weeks, top discretionary spending,
        recurring bills and income due in the next 7 days, the balance outlook (including the
        lowest expected balance and when), and the most important open anomalies."""
        return _query(
            ctx,
            lambda session, state: queries.weekly_summary(
                session,
                entity_id,
                today=state.clock().date(),
                week_of=week_of,
                settings=state.settings,
            ),
        )

    @server.tool(annotations=READ_ONLY)
    def get_forecast(
        entity_id: EntityId,
        ctx: ToolContext,
        horizon_days: Annotated[int, Field(description="30, 60, or 90.")] = 30,
    ) -> ForecastOut:
        """Daily expected balance with an 80% band for the next `horizon_days`, plus honest
        backtest accuracy: balance_error is the typical gap between forecast and actual
        balance, and coverage is how often the actual balance stayed inside the band."""
        return _query(
            ctx,
            lambda session, state: queries.get_forecast(
                session, entity_id, horizon_days, state.settings
            ),
        )

    @server.tool(annotations=READ_ONLY)
    def list_anomalies(
        entity_id: EntityId,
        ctx: ToolContext,
        status: Annotated[
            AnomalyStatus,
            Field(description="open (default), dismissed by a person, or resolved by a rescan."),
        ] = AnomalyStatus.OPEN,
    ) -> list[AnomalyOut]:
        """Flagged problems, most severe first: duplicate charges, category spending spikes,
        large first charges from new vendors, missed recurring bills or income, and recurring
        price changes. Each has a plain-English explanation."""
        return _query(ctx, lambda session, _: queries.list_anomalies(session, entity_id, status))

    @server.tool(annotations=READ_ONLY)
    def explain_anomaly(
        entity_id: EntityId,
        anomaly_id: Annotated[int, Field(description="Anomaly id, from list_anomalies.")],
        ctx: ToolContext,
    ) -> AnomalyDetail:
        """One anomaly with the transactions behind it (for a missed bill, the last charge
        that did arrive), to show exactly why it was flagged."""
        return _query(
            ctx, lambda session, _: queries.explain_anomaly(session, entity_id, anomaly_id)
        )

    @server.tool(annotations=READ_ONLY)
    def spend_by_category(
        entity_id: EntityId,
        start: Annotated[date, Field(description="First day, inclusive.")],
        end: Annotated[date, Field(description="Last day, inclusive.")],
        ctx: ToolContext,
    ) -> SpendByCategory:
        """Money out and money in per category between two dates, largest spending first,
        with overall totals."""
        return _query(
            ctx, lambda session, _: queries.spend_by_category(session, entity_id, start, end)
        )

    @server.tool(annotations=READ_ONLY)
    def list_recurring(entity_id: EntityId, ctx: ToolContext) -> list[RecurringOut]:
        """Detected subscriptions, bills, and regular income: cadence, typical amount (negative
        for bills), last seen, and next expected date, in order of next expected date."""
        return _query(ctx, lambda session, _: queries.list_recurring(session, entity_id))
