"""
Rule-based scorer for cycling race notifications.
"""
import logging
from dataclasses import dataclass
from typing import Optional

from db.database import EventType, FinishType, StageProfile, TickerEvent

logger = logging.getLogger(__name__)

DEFAULT_THRESHOLD = 70.0
MIN_THRESHOLD = 55.0
MAX_THRESHOLD = 90.0

_current_threshold: float = DEFAULT_THRESHOLD


def get_current_threshold() -> float:
    return _current_threshold


def set_threshold(value: float) -> None:
    global _current_threshold
    _current_threshold = value

# Cooldown in seconds between notifications (unless crash)
NOTIFICATION_COOLDOWN = 15 * 60


@dataclass
class ScoreResult:
    score: float
    notify_in_seconds: int
    reasons: list[str]
    should_notify: bool


def score_situation(
    recent_events: list[TickerEvent],
    stage_profile: Optional[StageProfile],
    current_threshold: float = DEFAULT_THRESHOLD,
) -> ScoreResult:
    """
    Score the current race situation and decide whether to notify.

    Args:
        recent_events: Last 10 ticker events (newest first).
        stage_profile: Stage profile with cols, finish_km, etc.
        current_threshold: Dynamic notification threshold.

    Returns:
        ScoreResult with score, notify_in_seconds, reasons, should_notify.
    """
    if not recent_events:
        return ScoreResult(score=0, notify_in_seconds=180, reasons=[], should_notify=False)

    latest = recent_events[0]
    km_remaining = latest.km_remaining
    finish_type = None
    cols = []
    finish_km = None

    if stage_profile:
        finish_type = stage_profile.race.finish_type if stage_profile.race else None
        cols = stage_profile.cols or []
        finish_km = stage_profile.finish_km

    event_types = [e.event_type for e in recent_events]
    score = 0.0
    reasons: list[str] = []
    notify_in_seconds = 180

    # --- Generic rules (always apply) ---
    if EventType.attack in event_types:
        score += 40
        reasons.append("aanval gedetecteerd (+40)")

    if EventType.crash in event_types:
        crash_bonus = 20
        if km_remaining is not None and km_remaining <= 30:
            crash_bonus += 15
        score += crash_bonus
        reasons.append(f"crash gedetecteerd (+{crash_bonus})")
        notify_in_seconds = 0  # send immediately on crash

    if km_remaining is not None:
        if km_remaining <= 10:
            score += 40  # 25 + 15
            reasons.append("< 10 km te gaan (+40)")
        elif km_remaining <= 30:
            score += 25
            reasons.append("< 30 km te gaan (+25)")

    # --- Finish-type specific rules ---
    if finish_type == FinishType.sprint:
        score, notify_in_seconds, reasons = _score_sprint(
            recent_events, event_types, km_remaining, score, notify_in_seconds, reasons
        )
    elif finish_type in (FinishType.uphill_finish, FinishType.mountain):
        score, notify_in_seconds, reasons = _score_mountain(
            recent_events, event_types, km_remaining, cols,
            score, notify_in_seconds, reasons
        )
    elif finish_type == FinishType.hill:
        score, notify_in_seconds, reasons = _score_hill(
            recent_events, event_types, km_remaining, cols,
            score, notify_in_seconds, reasons
        )

    score = min(score, 100.0)
    should_notify = score >= current_threshold

    logger.debug(
        "Score: %.1f (threshold %.1f) | %s",
        score, current_threshold, " | ".join(reasons)
    )

    return ScoreResult(
        score=score,
        notify_in_seconds=notify_in_seconds,
        reasons=reasons,
        should_notify=should_notify,
    )


def _score_sprint(
    events, event_types, km_remaining, score, notify_in_seconds, reasons
) -> tuple[float, int, list[str]]:
    recent_raw = " ".join(e.raw_text.lower() for e in events[:5])

    if km_remaining is not None and km_remaining <= 5:
        if EventType.caught in event_types:
            score += 60
            reasons.append("vlucht bijgehaald + ≤5 km (+60)")
            notify_in_seconds = 180
        else:
            score += 40
            reasons.append("≤ 5 km sprint-finale (+40)")
            notify_in_seconds = 180

    if EventType.crash in event_types and km_remaining is not None and km_remaining <= 10:
        score += 50
        reasons.append("crash in sprint-aanloop (+50)")
        notify_in_seconds = 0

    if "leadout" in recent_raw or "treintje" in recent_raw:
        score += 40
        reasons.append("leadout/treintje gedetecteerd (+40)")

    if km_remaining is not None and km_remaining <= 30:
        # No large gap = tension
        if EventType.gap not in event_types:
            score += 20
            reasons.append("spanning in finale (geen grote voorsprong) (+20)")

    return score, notify_in_seconds, reasons


def _score_mountain(
    events, event_types, km_remaining, cols,
    score, notify_in_seconds, reasons
) -> tuple[float, int, list[str]]:
    # Find the hardest col (HC first, then cat-1)
    hardest_col = _find_hardest_col(cols)

    if hardest_col and hardest_col.get("km_position") is not None and km_remaining is not None:
        col_km = hardest_col["km_position"]
        # Convert col km_position from-start to km_remaining
        # km_remaining_at_col = finish_km - col_km_position (approx.)
        distance_to_col = abs(km_remaining - col_km)
        if distance_to_col < 3:
            score += 55
            reasons.append(f"bergtop {hardest_col['name']} nadert (+55)")
            notify_in_seconds = 300  # 5 min earlier

    # Attack or solo on or after decisive col
    if EventType.attack in event_types or "solo" in " ".join(e.raw_text.lower() for e in events[:5]):
        if hardest_col and km_remaining is not None:
            col_km = hardest_col.get("km_position") or 0
            if km_remaining <= col_km:
                score += 65
                reasons.append("aanval/solo voorbij beslissende col (+65)")

    # Rapidly shrinking gap (gap event with decreasing mention)
    if EventType.gap in event_types:
        gap_texts = [e.raw_text for e in events if e.event_type == EventType.gap]
        if _detect_shrinking_gap(gap_texts):
            score += 40
            reasons.append("tijdsverschil daalt snel (+40)")

    # Summit on decisive col
    if EventType.summit in event_types:
        score += 50
        reasons.append("col-top passage (+50)")

    return score, notify_in_seconds, reasons


def _score_hill(
    events, event_types, km_remaining, cols,
    score, notify_in_seconds, reasons
) -> tuple[float, int, list[str]]:
    # Last meaningful col (cat-2 or higher)
    last_hard_col = _find_last_hard_col(cols, min_category=2)

    if last_hard_col and last_hard_col.get("km_position") is not None and km_remaining is not None:
        col_km = last_hard_col["km_position"]
        if abs(km_remaining - col_km) < 2:
            score += 50
            reasons.append(f"begin van {last_hard_col['name']} (+50)")

    if EventType.attack in event_types and km_remaining is not None and km_remaining <= 20:
        score += 45
        reasons.append("aanval in heuvelfinale (+45)")

    return score, notify_in_seconds, reasons


def _find_hardest_col(cols: list[dict]) -> Optional[dict]:
    """Return HC col, then cat-1, else None."""
    for cat in ["HC", "1"]:
        for col in cols:
            if col.get("category") == cat:
                return col
    return cols[0] if cols else None


def _find_last_hard_col(cols: list[dict], min_category: int = 2) -> Optional[dict]:
    """Return the last col of category <= min_category (HC/1/2)."""
    hard_cols = []
    for col in cols:
        cat = col.get("category", "")
        if cat == "HC" or (cat.isdigit() and int(cat) <= min_category):
            hard_cols.append(col)
    if not hard_cols:
        return None
    # Return the one with highest km_position (latest in race)
    return max(
        hard_cols,
        key=lambda c: c.get("km_position") or 0,
    )


def _detect_shrinking_gap(gap_texts: list[str]) -> bool:
    """Detect if gap is shrinking by comparing numbers in successive gap events."""
    import re
    gaps = []
    for text in gap_texts[:4]:
        nums = re.findall(r"\d+", text)
        if nums:
            # Take first significant number as gap (seconds/minutes)
            gaps.append(int(nums[0]))
    if len(gaps) >= 2:
        return gaps[0] < gaps[-1]  # most recent gap < earlier gap
    return False


def calculate_new_threshold(
    current_threshold: float,
    feedback_records: list,
) -> float:
    """
    Adjust notification threshold based on user feedback.
    Called after every 20 feedbacks.
    """
    from db.database import FeedbackType

    total = len(feedback_records)
    if total < 20:
        return current_threshold

    negative = sum(
        1 for f in feedback_records
        if f.feedback in (FeedbackType.too_early, FeedbackType.too_late, FeedbackType.unnecessary)
    )
    positive = sum(1 for f in feedback_records if f.feedback == FeedbackType.good)

    if total > 0 and negative / total > 0.5:
        new_threshold = min(current_threshold + 5, MAX_THRESHOLD)
        logger.info(
            "Threshold raised to %.0f (%.0f%% negative feedback)",
            new_threshold, 100 * negative / total,
        )
        return new_threshold

    if total > 0 and positive / total > 0.6:
        new_threshold = max(current_threshold - 3, MIN_THRESHOLD)
        logger.info(
            "Threshold lowered to %.0f (%.0f%% positive feedback)",
            new_threshold, 100 * positive / total,
        )
        return new_threshold

    return current_threshold
