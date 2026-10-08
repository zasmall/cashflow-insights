"""The only module that talks to statsforecast. Everything else sees plain typed tuples."""

from collections.abc import Sequence
from dataclasses import dataclass
from statistics import fmean
from typing import Self

import polars as pl
from statsforecast import StatsForecast
from statsforecast.models import AutoETS, HistoricAverage, SeasonalNaive

from cashflow.core.enums import ForecastModel

WEEK = 7


@dataclass(frozen=True)
class ModelForecast:
    """Per-day forecast of one series: mean and a central prediction interval."""

    mean: tuple[float, ...]
    lower: tuple[float, ...]
    upper: tuple[float, ...]

    @classmethod
    def flat(cls, horizon: int, value: float = 0.0) -> Self:
        return cls((value,) * horizon, (value,) * horizon, (value,) * horizon)


def _build(model: ForecastModel) -> AutoETS | SeasonalNaive | HistoricAverage:
    match model:
        case ForecastModel.AUTO_ETS:
            return AutoETS(season_length=WEEK, alias=model.value)
        case ForecastModel.SEASONAL_NAIVE:
            return SeasonalNaive(season_length=WEEK, alias=model.value)
        case ForecastModel.HISTORIC_AVERAGE:
            return HistoricAverage(alias=model.value)


def fit_predict(
    history: pl.DataFrame, *, horizon: int, level: int, models: Sequence[ForecastModel]
) -> dict[ForecastModel, ModelForecast]:
    """Fit each model to a gap-free daily series (`ds`, `y`) and forecast `horizon` days.

    A series without variation (empty, or all one value) needs no model and would make some of
    them fail, so it is forecast as that constant with zero-width intervals.
    """
    if history.height < 2 * WEEK or history["y"].n_unique() == 1:
        value = fmean(history["y"].to_list()) if history.height else 0.0
        return {m: ModelForecast.flat(horizon, value) for m in models}

    frame = history.select(pl.lit("series").alias("unique_id"), "ds", "y")
    engine = StatsForecast(models=[_build(m) for m in models], freq="1d")
    out = engine.forecast(df=frame, h=horizon, level=[level])
    if not isinstance(out, pl.DataFrame):  # statsforecast mirrors the input frame type
        raise TypeError(f"expected a Polars frame from statsforecast, got {type(out).__name__}")

    return {
        m: ModelForecast(
            mean=tuple(out[m.value].to_list()),
            lower=tuple(out[f"{m.value}-lo-{level}"].to_list()),
            upper=tuple(out[f"{m.value}-hi-{level}"].to_list()),
        )
        for m in models
    }
