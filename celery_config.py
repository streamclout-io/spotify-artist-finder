# celery_config.py
from celery import Celery
import os
from dotenv import load_dotenv
import multiprocessing
from config.rate_limits import get_celery_rate_limit

load_dotenv(override=False)  # Don't override Docker environment variables

# Initialize Celery app
celery_app = Celery(
    'spotify_tasks',
    broker=os.getenv('REDIS_URL', 'redis://localhost:6379/0'),
    include=['tasks']  # Explicitly include the tasks module
)

# Configuration
celery_app.conf.update(
    task_serializer='json',
    accept_content=['json'],
    result_serializer='json',
    timezone='UTC',
    worker_concurrency=multiprocessing.cpu_count() * 2,
    task_annotations={
        'tasks.search_artist_string': {
            'rate_limit': get_celery_rate_limit()
        }
    },
)

# Beat schedule
celery_app.conf.beat_schedule = {
    'generate-search-strings': {
        'task': 'tasks.generate_search_strings',
        'schedule': 5,
    },
}

# This ensures the tasks are registered
if __name__ == '__main__':
    celery_app.start()