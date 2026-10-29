from ..celery_app import celery_app
import time

__test__ = False

@celery_app.task
def test_task(word: str):
    time.sleep(1)
    return f"Hello {word}"


test_task.__test__ = False
