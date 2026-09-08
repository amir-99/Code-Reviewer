from uuid import uuid4

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from reviewer.orchestrator.states import TERMINAL, check_transition
from reviewer.store.models import Project, Review, ReviewEvent, ReviewStage, utcnow


class Store:
    def __init__(self, url: str):
        self.engine = create_async_engine(url, pool_pre_ping=True)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def get(self, review_id: str):
        async with self.sessions() as session:
            return await session.get(Review, review_id)

    async def accept(
        self, project_id: int, iid: int, sha: str, event_id: str, overrides=None
    ):
        async with self.sessions.begin() as session:
            # PostgreSQL row locking serializes admission per project. Projects
            # are pre-provisioned by startup, never created from untrusted hooks.
            project = await session.scalar(
                select(Project)
                .where(Project.gitlab_project_id == project_id)
                .with_for_update()
            )
            if project is None:
                raise ValueError("Project is not configured")
            existing = await session.scalar(
                select(Review).where(Review.event_id == event_id)
            )
            if existing:
                return existing
            active = await session.scalar(
                select(Review)
                .where(
                    Review.project_id == project.id,
                    Review.mr_iid == iid,
                    Review.state.not_in([str(x) for x in TERMINAL]),
                )
                .with_for_update()
            )
            review_id = str(uuid4())
            if active:
                active.state = "SUPERSEDED"
                active.history = active.history + ["SUPERSEDED"]
                active.finished_at = utcnow()
                active.superseded_by = review_id
                await self._event(session, active.id, "state", {"state": "SUPERSEDED"})
                await session.flush()
            review = Review(
                id=review_id,
                event_id=event_id,
                project_id=project.id,
                mr_iid=iid,
                head_sha=sha,
                overrides=overrides,
            )
            session.add(review)
            await session.flush()
            await self._event(session, review.id, "state", {"state": "INIT"})
            return review

    async def transition(self, review_id: str, state: str, **fields):
        async with self.sessions.begin() as session:
            review = await session.scalar(
                select(Review).where(Review.id == review_id).with_for_update()
            )
            if review.state in TERMINAL:
                return review
            check_transition(review.state, state)
            review.state = str(state)
            review.history = review.history + [str(state)]
            await self._event(session, review.id, "state", {"state": str(state)})
            if state in TERMINAL:
                review.finished_at = utcnow()
            for name, value in fields.items():
                setattr(review, name, value)
            return review

    async def mark_status(self, review_id: str):
        async with self.sessions.begin() as session:
            review = await session.get(Review, review_id)
            review.status_delivered = True

    async def noop(self, review_id: str):
        async with self.sessions.begin() as session:
            if not await session.get(ReviewStage, (review_id, "noop")):
                session.add(
                    ReviewStage(
                        review_id=review_id, stage="noop", status="passed", attempts=1
                    )
                )

    async def provision(self, ids):
        async with self.sessions.begin() as session:
            for project_id in ids:
                if not await session.scalar(
                    select(Project).where(Project.gitlab_project_id == project_id)
                ):
                    session.add(Project(gitlab_project_id=project_id))

    async def cancel(self, project_id: int, iid: int):
        async with self.sessions() as session:
            ids = (
                await session.scalars(
                    select(Review.id)
                    .join(Project)
                    .where(
                        Project.gitlab_project_id == project_id,
                        Review.mr_iid == iid,
                        Review.state.not_in([str(x) for x in TERMINAL]),
                    )
                )
            ).all()
        for review_id in ids:
            await self.transition(review_id, "CANCELLED")

    async def pending(self):
        async with self.sessions() as session:
            return (
                await session.scalars(
                    select(Review).where(
                        (Review.status_delivered.is_(False))
                        | (Review.state.not_in([str(x) for x in TERMINAL]))
                    )
                )
            ).all()

    async def is_configured(self, gitlab_project_id: int) -> bool:
        async with self.sessions() as session:
            return (
                await session.scalar(
                    select(Project).where(
                        Project.gitlab_project_id == gitlab_project_id
                    )
                )
            ) is not None

    async def project_number(self, review):
        async with self.sessions() as session:
            return (await session.get(Project, review.project_id)).gitlab_project_id

    async def save_snapshot(self, review_id, data):
        from reviewer.store.models import ReviewSnapshot

        async with self.sessions.begin() as session:
            row = await session.get(ReviewSnapshot, review_id)
            if row:
                row.data = data
            else:
                session.add(ReviewSnapshot(review_id=review_id, data=data))

    async def snapshot(self, review_id):
        from reviewer.store.models import ReviewSnapshot

        async with self.sessions() as session:
            row = await session.get(ReviewSnapshot, review_id)
            return row.data if row else None

    async def previous(self, project_id, iid, exclude):
        from reviewer.store.models import ReviewSnapshot

        async with self.sessions() as session:
            return await session.scalar(
                select(ReviewSnapshot.data)
                .join(Review)
                .where(
                    Review.project_id == project_id,
                    Review.mr_iid == iid,
                    Review.id != exclude,
                    Review.state == "PUBLISHED",
                )
                .order_by(Review.finished_at.desc())
                .limit(1)
            )

    async def save_stage(self, review_id, result):
        async with self.sessions.begin() as session:
            row = await session.get(ReviewStage, (review_id, result.stage))
            data = dict(
                status="failed" if result.failed else "passed",
                attempts=result.attempts,
                coverage_json=result.model_dump(mode="json"),
            )
            if row:
                for k, v in data.items():
                    setattr(row, k, v)
            else:
                session.add(
                    ReviewStage(review_id=review_id, stage=result.stage, **data)
                )

    async def stages(self, review_id):
        from reviewer.orchestrator.stages import StageResult

        async with self.sessions() as session:
            rows = (
                await session.scalars(
                    select(ReviewStage).where(
                        ReviewStage.review_id == review_id, ReviewStage.stage != "noop"
                    )
                )
            ).all()
            return {r.stage: StageResult.model_validate(r.coverage_json) for r in rows}

    async def save_findings(self, review, findings):
        from reviewer.store.models import FindingRow

        async with self.sessions.begin() as session:
            for f in findings:
                row = await session.get(FindingRow, f.id)
                data = dict(
                    review_id=review.id,
                    project_id=review.project_id,
                    mr_iid=review.mr_iid,
                    fingerprint=f.fingerprint,
                    data=f.model_dump(mode="json"),
                    severity_final=f.severity_final,
                    status=f.status,
                    file=f.anchor.file,
                )
                if row:
                    for k, v in data.items():
                        setattr(row, k, v)
                else:
                    session.add(FindingRow(id=f.id, **data))

    async def record_comment(self, finding_id, discussion):
        from reviewer.store.models import PublishedComment

        async with self.sessions.begin() as session:
            if not await session.get(PublishedComment, finding_id):
                session.add(
                    PublishedComment(
                        finding_id=finding_id,
                        discussion_id=discussion.id,
                        note_id=str(discussion.notes[0].id),
                    )
                )

    async def outcome(self, project_id, iid, fingerprint, outcome, reason, user):
        from reviewer.store.models import FindingOutcome

        async with self.sessions.begin() as session:
            # Command retries must not count the same feedback repeatedly.
            exists = await session.scalar(
                select(FindingOutcome).where(
                    FindingOutcome.project_id == project_id,
                    FindingOutcome.mr_iid == iid,
                    FindingOutcome.fingerprint == fingerprint,
                    FindingOutcome.outcome == outcome,
                )
            )
            if not exists:
                session.add(
                    FindingOutcome(
                        project_id=project_id,
                        mr_iid=iid,
                        fingerprint=fingerprint,
                        outcome=outcome,
                        reason=reason[:1000],
                        labelled_by=str(user),
                    )
                )

    async def by_event(self, event_id: str):
        """The review a queued event produced, once the worker has admitted it."""
        async with self.sessions() as session:
            return await session.scalar(
                select(Review).where(Review.event_id == event_id)
            )

    async def findings_for(self, review_id):
        """Stored findings for one review, newest severity data included."""
        from reviewer.store.models import FindingRow

        async with self.sessions() as session:
            rows = (
                await session.scalars(
                    select(FindingRow).where(FindingRow.review_id == review_id)
                )
            ).all()
            return [row.data for row in rows]

    async def finding_by_fingerprint(self, project_id, iid, fingerprint):
        from reviewer.findings.models import Finding
        from reviewer.store.models import FindingRow

        async with self.sessions() as session:
            row = await session.scalar(
                select(FindingRow)
                .join(Project, FindingRow.project_id == Project.id)
                .join(Review, FindingRow.review_id == Review.id)
                .where(
                    Project.gitlab_project_id == project_id,
                    FindingRow.mr_iid == iid,
                    FindingRow.fingerprint == fingerprint,
                )
                .order_by(Review.started_at.desc())
                .limit(1)
            )
            return Finding.model_validate(row.data) if row else None

    async def _event(self, session, review_id, kind, data):
        # Callers hold the review row lock, so sequence order is commit order.
        sequence = await session.scalar(
            select(func.coalesce(func.max(ReviewEvent.sequence), 0)).where(
                ReviewEvent.review_id == review_id
            )
        )
        session.add(
            ReviewEvent(
                review_id=review_id, sequence=sequence + 1, kind=kind, data=data
            )
        )

    async def append_event(self, review_id, kind, data):
        async with self.sessions.begin() as session:
            # SQLite has no row locks; acquire its writer lock before reading the
            # sequence. PostgreSQL serializes writers with FOR UPDATE below.
            if self.engine.dialect.name == "sqlite":
                await session.execute(text("BEGIN IMMEDIATE"))
            review = await session.scalar(
                select(Review).where(Review.id == review_id).with_for_update()
            )
            if review is not None:
                await self._event(session, review_id, kind, data)

    async def events(self, review_id, after=0, limit=200):
        async with self.sessions() as session:
            rows = (
                await session.scalars(
                    select(ReviewEvent)
                    .where(
                        ReviewEvent.review_id == review_id, ReviewEvent.sequence > after
                    )
                    .order_by(ReviewEvent.sequence)
                    .limit(limit)
                )
            ).all()
            return [
                dict(
                    id=r.sequence, kind=r.kind, data=r.data, at=r.created_at.isoformat()
                )
                for r in rows
            ]

    async def recent(self, limit=50, offset=0):
        async with self.sessions() as session:
            rows = (
                await session.execute(
                    select(Review, Project.gitlab_project_id)
                    .join(Project)
                    .order_by(Review.started_at.desc(), Review.id)
                    .limit(limit)
                    .offset(offset)
                )
            ).all()
            return [
                dict(
                    id=r.id,
                    project_id=p,
                    mr_iid=r.mr_iid,
                    head_sha=r.head_sha,
                    state=r.state,
                    partial=r.partial,
                    decision=r.decision,
                    started_at=r.started_at,
                )
                for r, p in rows
            ]
