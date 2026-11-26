"""Route risk metrics and ranking. Pure functions: no Django, no HTTP."""

from collections.abc import Sequence
from dataclasses import dataclass

from trips.risk import RiskLevel


@dataclass(frozen=True)
class RouteMetrics:
    miles_by_level: dict[RiskLevel, float]
    average_risk: float
    max_level: RiskLevel


@dataclass(frozen=True)
class RankInput:
    route_id: str
    metrics: RouteMetrics
    duration_seconds: int


@dataclass(frozen=True)
class RankedPlan:
    order: list[str]  # route ids, best first
    recommended_route_id: str | None
    no_safe_route: bool
    reason: str


NO_SAFE_ROUTE_REASON = "Every route has No Travel conditions at this departure time."

_REASONS = (
    "No No Travel miles ({:.1f} vs {:.1f})",
    "Fewest Severe miles ({:.1f} vs {:.1f})",
    "Fewest High miles ({:.1f} vs {:.1f})",
    "Lowest average risk ({:.2f} vs {:.2f})",
    "Shortest travel time ({} vs {})",
)


def coverage(distances: Sequence[float], total_miles: float) -> list[tuple[float, float]]:
    """Each checkpoint covers halfway to its neighbours, clipped to the route ends."""
    last = len(distances) - 1
    ranges = []
    for i, d in enumerate(distances):
        start = 0.0 if i == 0 else (distances[i - 1] + d) / 2
        end = total_miles if i == last else (d + distances[i + 1]) / 2
        ranges.append((max(0.0, start), min(total_miles, end)))
    return ranges


def summarize(
    distances: Sequence[float], levels: Sequence[RiskLevel], total_miles: float
) -> RouteMetrics:
    miles = dict.fromkeys(RiskLevel, 0.0)
    for (start, end), level in zip(coverage(distances, total_miles), levels, strict=True):
        miles[level] += end - start
    weighted = sum(level * covered for level, covered in miles.items())
    return RouteMetrics(
        miles_by_level=miles,
        average_risk=weighted / total_miles if total_miles > 0 else 0.0,
        max_level=max(levels),
    )


def _sort_key(route: RankInput) -> tuple[float, float, float, float, int]:
    miles = route.metrics.miles_by_level
    return (
        round(miles[RiskLevel.NO_TRAVEL], 1),
        round(miles[RiskLevel.SEVERE], 1),
        round(miles[RiskLevel.HIGH], 1),
        round(route.metrics.average_risk, 2),
        route.duration_seconds,
    )


def format_duration(seconds: int) -> str:
    hours, minutes = divmod(round(seconds / 60), 60)
    return f"{hours} h {minutes} min"


def rank(routes: Sequence[RankInput]) -> RankedPlan:
    """Order by (No Travel, Severe, High miles, average risk, duration); ties keep input order."""
    ordered = sorted(routes, key=_sort_key)
    order = [r.route_id for r in ordered]
    best = _sort_key(ordered[0])
    if best[0] > 0:
        return RankedPlan(order, None, True, NO_SAFE_ROUTE_REASON)
    if len(ordered) == 1:
        return RankedPlan(order, order[0], False, "Only route found")

    runner_up = _sort_key(ordered[1])
    reason = "Equal on every criterion; first route returned"
    for i, (a, b) in enumerate(zip(best, runner_up, strict=True)):
        if a < b:
            if i == len(_REASONS) - 1:
                a, b = format_duration(a), format_duration(b)
            reason = _REASONS[i].format(a, b)
            break
    return RankedPlan(order, order[0], False, reason)
