"""Hidden `.factory/loop/` control-plane package (factory-plan/v1 and later)."""

from .plan_parser import (
    ALLOWED_TRANSITIONS,
    CLASSIFICATIONS,
    FINAL_AUDIT_TITLE,
    LIFECYCLE_STATUSES,
    MATRIX_HEADER,
    Plan,
    PlanError,
    SCHEMA_NAME,
    TASK_STATUSES,
    is_allowed_transition,
    parse_plan,
    round_trip,
)

__all__ = [
    "ALLOWED_TRANSITIONS",
    "CLASSIFICATIONS",
    "FINAL_AUDIT_TITLE",
    "LIFECYCLE_STATUSES",
    "MATRIX_HEADER",
    "Plan",
    "PlanError",
    "SCHEMA_NAME",
    "TASK_STATUSES",
    "is_allowed_transition",
    "parse_plan",
    "round_trip",
]
