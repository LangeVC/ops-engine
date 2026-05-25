"""CORE-007: QueueManager v2 — Backpressure, retries, DLQ, metrics, graceful shutdown."""

import asyncio
import logging
import time
from typing import Any, Callable, Awaitable

from ops_engine.adapters.base import ForgeAdapter

logger = logging.getLogger(__name__)

EventHandler = Callable[[ForgeAdapter, dict[str, Any]], Awaitable[None]]


class QueueMetrics:
    """Tracks queue performance metrics."""

    def __init__(self) -> None:
        self.enqueued: int = 0
        self.processed: int = 0
        self.errors: int = 0
        self.rejected: int = 0
        self.dead_lettered: int = 0
        self._processing_times: list[float] = []

    @property
    def avg_processing_time_ms(self) -> float:
        if not self._processing_times:
            return 0.0
        return (sum(self._processing_times) / len(self._processing_times)) * 1000

    def record_processing_time(self, duration: float) -> None:
        self._processing_times.append(duration)
        if len(self._processing_times) > 1000:
            self._processing_times = self._processing_times[-1000:]

    def to_dict(self) -> dict[str, Any]:
        return {
            "enqueued": self.enqueued,
            "processed": self.processed,
            "errors": self.errors,
            "rejected": self.rejected,
            "dead_lettered": self.dead_lettered,
            "avg_processing_time_ms": round(self.avg_processing_time_ms, 2),
        }


class QueueManager:
    """Manages an asynchronous queue to process webhook events sequentially
    and enforces rate limits on API calls.

    v2: backpressure (bounded queue), retry with DLQ, metrics, graceful shutdown.
    """

    def __init__(
        self,
        rate_limit_delay_seconds: float = 1.0,
        max_queue_size: int = 1000,
        max_retries: int = 3,
    ):
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=max_queue_size)
        self.dead_letter_queue: asyncio.Queue = asyncio.Queue()
        self.rate_limit_delay = rate_limit_delay_seconds
        self.max_retries = max_retries
        self.metrics = QueueMetrics()
        self._worker_task: asyncio.Task | None = None
        self._shutting_down = False

    async def start(self) -> None:
        if not self._worker_task:
            self._shutting_down = False
            self._worker_task = asyncio.create_task(self._worker())
            logger.info("QueueManager worker started.")

    async def stop(self) -> None:
        """Graceful shutdown: drain remaining items, then cancel worker."""
        if self._worker_task:
            self._shutting_down = True
            remaining = self.queue.qsize()
            if remaining > 0:
                logger.info(f"QueueManager draining {remaining} remaining items...")
                try:
                    await asyncio.wait_for(self.queue.join(), timeout=30.0)
                    logger.info("Queue drained successfully.")
                except asyncio.TimeoutError:
                    logger.warning("Queue drain timed out after 30s.")

            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
            self._worker_task = None
            logger.info("QueueManager worker stopped.")

    async def enqueue(
        self,
        adapter: ForgeAdapter,
        event: dict[str, Any],
        handler: EventHandler,
    ) -> bool:
        """Enqueue an event for processing.

        Returns True if enqueued, False if rejected (queue full or shutting down).
        """
        if self._shutting_down:
            self.metrics.rejected += 1
            return False

        try:
            self.queue.put_nowait((adapter, event, handler, 0))
            self.metrics.enqueued += 1
            logger.debug(
                f"Enqueued {event.get('event_type')}. Queue size: {self.queue.qsize()}"
            )
            return True
        except asyncio.QueueFull:
            logger.warning(f"Queue full ({self.queue.maxsize}). Rejecting {event.get('event_type')}")
            self.metrics.rejected += 1
            return False

    async def _worker(self) -> None:
        while True:
            try:
                adapter, event, handler, retry_count = await self.queue.get()
                start_time = time.monotonic()

                try:
                    await handler(adapter, event)
                    self.metrics.processed += 1
                    self.metrics.record_processing_time(time.monotonic() - start_time)
                except Exception as e:
                    self.metrics.errors += 1
                    logger.error(f"Error processing {event.get('event_type')}: {e}")

                    if retry_count + 1 < self.max_retries:
                        try:
                            self.queue.put_nowait((adapter, event, handler, retry_count + 1))
                        except asyncio.QueueFull:
                            await self._dead_letter(event, e)
                    else:
                        await self._dead_letter(event, e)

                self.queue.task_done()
                await asyncio.sleep(self.rate_limit_delay)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Unexpected error in queue worker: {e}")
                await asyncio.sleep(1)

    async def _dead_letter(self, event: dict[str, Any], error: Exception) -> None:
        self.metrics.dead_lettered += 1
        logger.error(f"Dead-lettered {event.get('event_type')} after {self.max_retries} retries: {error}")
        await self.dead_letter_queue.put({
            "event": event,
            "error": str(error),
            "timestamp": time.time(),
        })

    @property
    def queue_size(self) -> int:
        return self.queue.qsize()

    @property
    def dlq_size(self) -> int:
        return self.dead_letter_queue.qsize()
