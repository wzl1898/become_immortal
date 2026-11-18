"""Temporal Worker process managed by ``story serve``."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import os

from dotenv import load_dotenv
from temporalio.client import Client
from temporalio.worker import Worker

load_dotenv()

import run_store  # noqa: E402
import temporal_activities as activities  # noqa: E402
from temporal_workflows import DebugRerunWorkflow, TurnWorkflow  # noqa: E402


async def run_worker() -> None:
    await run_store.init()
    client = await Client.connect(
        os.getenv("TEMPORAL_ADDRESS", "127.0.0.1:7233"),
        namespace=os.getenv("TEMPORAL_NAMESPACE", "default"),
    )
    worker = Worker(
        client,
        task_queue=os.getenv("TEMPORAL_TASK_QUEUE", "become-immortal-turns"),
        max_heartbeat_throttle_interval=timedelta(seconds=1),
        default_heartbeat_throttle_interval=timedelta(seconds=1),
        workflows=[TurnWorkflow, DebugRerunWorkflow],
        activities=[
            activities.describe_run_activity,
            activities.initialize_debug_activity,
            activities.prepare_turn_activity,
            activities.narrative_activity,
            activities.postprocess_turn_activity,
            activities.commit_turn_activity,
            activities.fail_turn_activity,
            activities.finalize_cancel_activity,
        ],
    )
    try:
        await worker.run()
    finally:
        await run_store.close()


if __name__ == "__main__":
    asyncio.run(run_worker())
