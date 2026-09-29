"""
Thin Celery entry-point so `celery -A tasks worker` still works.

All actual logic lives in worker/.
"""

from worker.celery_app import celery_app as app          # noqa: F401
from worker.task import process_video_submission          # noqa: F401