"""Minimal Ralph factory control-plane package."""

# Core modules
from .plan_parser import Task, Plan, parse, dump
from .selector import SelectionResult, select
from .state import State, load, save, validate, SCHEMA
from .gitutil import (
    current_branch,
    current_commit,
    is_clean,
    commit_all,
    switch_branch,
    file_at_commit,
)
from .lock import Lock
from .runner import (
    Runner,
    VerificationResult,
    load_environment,
    check_runner_available,
    get_available_capabilities,
    run_verification,
    sync_to_runner,
)
from .preflight import PreflightResult, run_preflight
from .campaign import main

__all__ = [
    # plan_parser
    "Task", "Plan", "parse", "dump",
    # selector
    "SelectionResult", "select",
    # state
    "State", "load", "save", "validate", "SCHEMA",
    # gitutil
    "current_branch", "current_commit", "is_clean",
    "commit_all", "switch_branch", "file_at_commit",
    # lock
    "Lock",
    # runner
    "Runner", "VerificationResult", "load_environment",
    "check_runner_available", "get_available_capabilities",
    "run_verification", "sync_to_runner",
    # preflight
    "PreflightResult", "run_preflight",
    # campaign
    "main",
]