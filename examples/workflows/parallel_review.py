"""Parallel review then summary: the first configured example workflow.

Two independent review children run in parallel over the same task text; the
script returns their combined summary. This is trusted configured code, like
the plugin itself: task text and child outputs are data handled by the core's
governed execution, and no sandboxing or injection filtering is claimed here.
"""

import asyncio


async def run(ctx, task):
    async with asyncio.TaskGroup() as group:
        first = group.create_task(ctx.agent("review", f"Review A: {task}", key="review-a"))
        second = group.create_task(ctx.agent("review", f"Review B: {task}", key="review-b"))
    return f"combined: {first.result()} | {second.result()}"
