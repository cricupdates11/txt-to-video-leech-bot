import asyncio
import logging
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class Job:
    """Represents a single user's TXT-to-Video batch job."""
    job_id: str
    user_id: int
    username: str
    chat_id: int
    urls: List[str]
    status_message_id: int
    temp_dir: Path
    created_at: float = field(default_factory=time.time)
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)

    # Progress tracking counters
    state: str = "queued"  # queued | processing | cancelling | finished
    current_index: int = 0
    completed_count: int = 0
    failed_count: int = 0

    @property
    def total_urls(self) -> int:
        return len(self.urls)


class QueueManager:
    """
    Manages the asyncio job queue, enforces one-job-per-user isolation,
    spawns concurrent background workers, and cleans up temporary folders.
    """

    def __init__(
        self,
        max_concurrent_jobs: int,
        job_processor: Callable[[Job], Awaitable[None]],
    ) -> None:
        self.max_concurrent_jobs = max_concurrent_jobs
        self.job_processor = job_processor
        self.queue: asyncio.Queue[Job] = asyncio.Queue()
        self.active_user_jobs: Dict[int, Job] = {}
        self.workers: List[asyncio.Task] = []
        self._base_temp_dir = Path(tempfile.gettempdir()) / "txt_leech_bot_jobs"
        self._base_temp_dir.mkdir(parents=True, exist_ok=True)

    def has_active_job(self, user_id: int) -> bool:
        return user_id in self.active_user_jobs

    def get_user_job(self, user_id: int) -> Optional[Job]:
        return self.active_user_jobs.get(user_id)

    async def enqueue_job(
        self,
        user_id: int,
        username: str,
        chat_id: int,
        urls: List[str],
        status_message_id: int,
    ) -> Job:
        job_id = uuid.uuid4().hex[:8]
        job_temp_dir = self._base_temp_dir / f"job_{user_id}_{job_id}"
        job_temp_dir.mkdir(parents=True, exist_ok=True)

        job = Job(
            job_id=job_id,
            user_id=user_id,
            username=username,
            chat_id=chat_id,
            urls=urls,
            status_message_id=status_message_id,
            temp_dir=job_temp_dir,
        )
        self.active_user_jobs[user_id] = job
        await self.queue.put(job)
        logger.info(
            "Enqueued job %s for user %s (%d URLs). Queue size: %d",
            job.job_id,
            user_id,
            len(urls),
            self.queue.qsize(),
        )
        return job

    def cancel_user_job(self, user_id: int) -> Optional[Job]:
        job = self.active_user_jobs.get(user_id)
        if not job:
            return None
        job.state = "cancelling"
        job.cancel_event.set()
        return job

    async def start_workers(self) -> None:
        """Start the configured number of background worker tasks."""
        if self.workers:
            return
        for i in range(self.max_concurrent_jobs):
            task = asyncio.create_task(self._worker_loop(i + 1), name=f"job-worker-{i+1}")
            self.workers.append(task)
        logger.info("Started %d background queue workers.", self.max_concurrent_jobs)

    async def stop_workers(self) -> None:
        """Cancel all active jobs, stop workers, and clean up temporary directories."""
        logger.info("Stopping queue workers and cleaning up temporary directories...")
        for job in list(self.active_user_jobs.values()):
            job.cancel_event.set()

        for worker in self.workers:
            worker.cancel()

        if self.workers:
            await asyncio.gather(*self.workers, return_exceptions=True)
        self.workers.clear()

        shutil.rmtree(self._base_temp_dir, ignore_errors=True)
        logger.info("All workers stopped and temporary files removed.")

    async def _worker_loop(self, worker_id: int) -> None:
        while True:
            job = await self.queue.get()
            try:
                if job.cancel_event.is_set():
                    logger.info("Worker %d skipping pre-cancelled job %s", worker_id, job.job_id)
                else:
                    job.state = "processing"
                    logger.info("Worker %d processing job %s for user %s", worker_id, job.job_id, job.user_id)
                    await self.job_processor(job)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Unhandled error in Worker %d for job %s: %s", worker_id, job.job_id, exc)
            finally:
                job.state = "finished"
                shutil.rmtree(job.temp_dir, ignore_errors=True)
                self.active_user_jobs.pop(job.user_id, None)
                self.queue.task_done()

