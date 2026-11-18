"""Deterministic Temporal workflows. Business data stays in the ledger."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy


ACTIVITY_TIMEOUT = timedelta(minutes=20)
HEARTBEAT_TIMEOUT = timedelta(seconds=30)
RETRY_THREE = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=10),
    maximum_attempts=3,
)


@workflow.defn
class TurnWorkflow:
    @workflow.run
    async def run(self, run_id: str) -> dict:
        try:
            await workflow.execute_activity(
                "prepare_turn_activity",
                run_id,
                start_to_close_timeout=ACTIVITY_TIMEOUT,
                heartbeat_timeout=HEARTBEAT_TIMEOUT,
                cancellation_type=workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
                retry_policy=RETRY_THREE,
            )
            await workflow.execute_activity(
                "narrative_activity",
                run_id,
                start_to_close_timeout=ACTIVITY_TIMEOUT,
                heartbeat_timeout=HEARTBEAT_TIMEOUT,
                cancellation_type=workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
                retry_policy=RETRY_THREE,
            )
            await workflow.execute_activity(
                "postprocess_turn_activity",
                run_id,
                start_to_close_timeout=ACTIVITY_TIMEOUT,
                heartbeat_timeout=HEARTBEAT_TIMEOUT,
                cancellation_type=workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
                retry_policy=RETRY_THREE,
            )
            return await workflow.execute_activity(
                "commit_turn_activity",
                run_id,
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await workflow.execute_activity(
                "fail_turn_activity",
                {"run_id": run_id, "error": {"type": type(exc).__name__, "message": str(exc)}},
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            raise


@workflow.defn
class DebugRerunWorkflow:
    @workflow.run
    async def run(self, run_id: str) -> dict:
        # Reused upstream nodes are represented by source_node_id in the ledger.
        # Activities write only into the debug branch and commit_turn_activity
        # deliberately completes it without touching the canonical save.
        try:
            description = await workflow.execute_activity(
                "describe_run_activity",
                run_id,
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RETRY_THREE,
            )
            fork_ordinal = int(description.get("fork_ordinal") or 0)
            if fork_ordinal <= 9:  # skeleton
                await workflow.execute_activity(
                    "prepare_turn_activity",
                    run_id,
                    start_to_close_timeout=ACTIVITY_TIMEOUT,
                    heartbeat_timeout=HEARTBEAT_TIMEOUT,
                    cancellation_type=workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
                    retry_policy=RETRY_THREE,
                )
            else:
                await workflow.execute_activity(
                    "initialize_debug_activity",
                    run_id,
                    start_to_close_timeout=timedelta(minutes=2),
                    retry_policy=RETRY_THREE,
                )
            if fork_ordinal <= 10:  # narrative
                await workflow.execute_activity(
                    "narrative_activity",
                    run_id,
                    start_to_close_timeout=ACTIVITY_TIMEOUT,
                    heartbeat_timeout=HEARTBEAT_TIMEOUT,
                    cancellation_type=workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
                    retry_policy=RETRY_THREE,
                )
            if fork_ordinal <= 16:  # next_event
                await workflow.execute_activity(
                    "postprocess_turn_activity",
                    run_id,
                    start_to_close_timeout=ACTIVITY_TIMEOUT,
                    heartbeat_timeout=HEARTBEAT_TIMEOUT,
                    cancellation_type=workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
                    retry_policy=RETRY_THREE,
                )
            return await workflow.execute_activity(
                "commit_turn_activity",
                run_id,
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await workflow.execute_activity(
                "fail_turn_activity",
                {"run_id": run_id, "error": {"type": type(exc).__name__, "message": str(exc)}},
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            raise
