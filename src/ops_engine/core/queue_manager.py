import asyncio
import logging
from typing import Any, Dict, Callable, Awaitable
from ops_engine.adapters.base import ForgeAdapter

logger = logging.getLogger(__name__)

EventHandler = Callable[[ForgeAdapter, Dict[str, Any]], Awaitable[None]]

class QueueManager:
    """
    Manages an asynchronous queue to process webhook events sequentially
    and enforces rate limits on the API calls.
    """
    def __init__(self, rate_limit_delay_seconds: float = 1.0):
        self.queue: asyncio.Queue = asyncio.Queue()
        self.rate_limit_delay = rate_limit_delay_seconds
        self._worker_task = None

    async def start(self):
        if not self._worker_task:
            self._worker_task = asyncio.create_task(self._worker())
            logger.info("OpsEngine QueueManager worker started.")

    async def stop(self):
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                logger.info("OpsEngine QueueManager worker stopped.")

    async def enqueue(self, adapter: ForgeAdapter, event: Dict[str, Any], handler: EventHandler):
        await self.queue.put((adapter, event, handler))
        logger.debug(f"Enqueued event {event.get('event_type')}. Queue size: {self.queue.qsize()}")

    async def _worker(self):
        while True:
            try:
                adapter, event, handler = await self.queue.get()
                logger.info(f"OpsEngine: Processing event from queue. Remaining: {self.queue.qsize()}")
                
                try:
                    await handler(adapter, event)
                except Exception as e:
                    logger.error(f"Error processing event {event.get('event_type')}: {e}")
                
                self.queue.task_done()
                await asyncio.sleep(self.rate_limit_delay)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Unexpected error in queue worker: {e}")
                await asyncio.sleep(1)
