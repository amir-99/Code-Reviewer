import pytest

from reviewer.services.forge.gitlab import FakeForge, MergeRequestContext
from reviewer.store.models import Base
from reviewer.store.repositories import Store


@pytest.fixture
async def store(tmp_path):
    store = Store(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    async with store.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await store.provision([7])
    yield store
    await store.engine.dispose()


@pytest.fixture
def forge():
    return FakeForge(MergeRequestContext(project_id=7, iid=2, head_sha="a" * 40))


class FakeQueue:
    def __init__(self):
        self.jobs = []
        self.ids = set()

    async def enqueue_job(self, name, *args, _job_id=None):
        if _job_id in self.ids:
            return None
        self.ids.add(_job_id)
        self.jobs.append((name, args))
        return _job_id

    async def ping(self):
        return True
