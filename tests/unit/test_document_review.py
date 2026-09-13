"""Document reviews: admission, corpus, anchoring, publication modes, controls."""

import base64
import json

import httpx
import pytest
from pydantic import SecretStr

from reviewer.accounts.credentials import Credentials
from reviewer.accounts.execution import PersonalDocs
from reviewer.config.schema import ProjectConfig, Settings
from reviewer.context.documents import DocumentCorpus, split_sections
from reviewer.context.models import ReviewOverrides
from reviewer.findings import document as policy
from reviewer.findings.document import (
    DocumentFinding,
)
from reviewer.findings.models import Provenance
from reviewer.orchestrator.document_pipeline import DocumentPipeline
from reviewer.publish.document_comments import DocumentComments
from reviewer.services.docs.confluence import (
    Confluence,
    DocumentContext,
    FakeDocumentService,
    InlineUnsupported,
    cql_string,
)
from reviewer.services.llm.client import FakeLLMClient
from reviewer.services.secrets.scanner import FakeSecretScanner
from reviewer.store.models import Account, ReviewTrigger

WIKI = "https://wiki.internal"
PAGE_URL = f"{WIKI}/pages/viewpage.action?pageId=100"
SUBJECT = {
    "page_id": "100",
    "space": "ENG",
    "title": "Payment API design",
    "url": PAGE_URL,
    "version": 4,
}
SUBJECT_TEXT = """Intro paragraph about the payment API design.

# Overview

The service retries failed charges three times before giving up.

## Limits

Requests are capped at 100 per second per tenant.

# Security

Tokens are stored in plain text in the database for auditing.
"""
RELATED_TEXT = """# Retry policy

The service retries failed charges five times before giving up.
"""


def page(page_id, title, text, url=None, space="ENG", version=4):
    return DocumentContext(
        page_id=page_id,
        space=space,
        title=title,
        url=url or f"{WIKI}/pages/viewpage.action?pageId={page_id}",
        version=version,
        text_md=text,
    )


def fake_docs():
    pages = {
        PAGE_URL: page("100", "Payment API design", SUBJECT_TEXT, PAGE_URL),
        f"{WIKI}/pages/viewpage.action?pageId=200": page(
            "200", "Retry policy", RELATED_TEXT
        ),
    }
    return FakeDocumentService(pages, user="doc-bot")


def proposal(**overrides):
    data = {
        "anchor": {
            "page_id": "100",
            "heading_path": "Overview",
            "quote": "retries failed charges three times",
        },
        "category": "consistency",
        "severity_proposed": "REQUIRED",
        "claim": "Retry count contradicts the retry policy page",
        "reason": "Overview says three, the policy page says five.",
        "impact": "Operators configure the wrong retry budget.",
        "impact_level": "MEDIUM",
        "suggested_direction": "Align the two pages on one retry count.",
        "confidence": "high",
        "related": [
            {
                "page_id": "200",
                "heading_path": "Retry policy",
                "quote": "retries failed charges five times",
            }
        ],
    }
    return data | overrides


def envelope(unit_id, findings, requests=()):
    return {
        "findings": findings,
        "context_requests": list(requests),
        "coverage": {
            "units_examined": [unit_id],
            "units_skipped": [],
            "skip_reason": None,
        },
        "notes_for_summary": "",
    }


def settings(tmp_path, **extra):
    s = Settings(
        **{
            "gateway_base_url": "https://gateway.internal/v1",
            "confluence_base_url": WIKI,
            "config_path": tmp_path / "missing.json",
        }
        | extra
    )
    s.credential_active_key = "test"
    s.credential_keys = {"test": SecretStr(base64.b64encode(b"k" * 32).decode())}
    return s


async def owner(store):
    async with store.transaction() as session:
        account = Account(
            login="owner", display_name="Owner", role="user", password_hash="set"
        )
        session.add(account)
        await session.flush()
        return account.id


async def admitted(store, user_id, **overrides):
    return await store.accept_document(
        SUBJECT,
        overrides.pop("event_id", "evt-doc"),
        ReviewOverrides(**overrides).model_dump(),
        owner_user_id=user_id,
        credential_refs={},
        principal_id="doc-bot",
    )


class Guard:
    async def __call__(self):
        return None


def personal(docs, owner_id, version=4):
    client = PersonalDocs(docs, Guard(), "doc-bot", "100", version)
    client.owner_user_id = owner_id
    return client


# -- storage -------------------------------------------------------------------


async def test_document_admission_supersedes_same_owner_and_conflicts_others(store):
    user_id = await owner(store)
    async with store.transaction() as session:
        other = Account(login="o2", display_name="O", role="user", password_hash="x")
        session.add(other)
        await session.flush()
        other_id = other.id
        session.add(
            ReviewTrigger(
                event_id="evt-other",
                owner_user_id=other_id,
                credential_refs={},
                payload={},
            )
        )
    first = await admitted(store, user_id)
    assert first.kind == "document" and first.subject_key == "confluence:100"
    assert first.project_id is None and first.head_sha == "4"
    # Redelivery returns the same row.
    assert (await admitted(store, user_id)).id == first.id
    second = await admitted(store, user_id, event_id="evt-doc-2")
    assert (await store.get(first.id)).state == "SUPERSEDED"
    assert (await store.get(first.id)).superseded_by == second.id
    conflict = await store.accept_document(
        SUBJECT,
        "evt-other",
        None,
        owner_user_id=other_id,
        credential_refs={},
        principal_id="p",
    )
    assert conflict is None
    async with store.sessions() as session:
        assert (await session.get(ReviewTrigger, "evt-other")).state == "CONFLICT"
    rows = await store.recent(10)
    assert rows[0]["kind"] == "document"
    assert rows[0]["subject"]["title"] == "Payment API design"
    assert await store.project_number(second) is None
    async with store.review_lock(second):
        pass


# -- Confluence client ---------------------------------------------------------


def test_cql_literals_are_escaped_and_bounded():
    assert cql_string('a "b" c\\d') == '"a \\"b\\" c\\\\d"'
    assert len(cql_string("x" * 1000)) == 202


async def test_confluence_comment_and_search_calls():
    seen = []

    def handler(request):
        seen.append(request)
        path = request.url.path
        if path.endswith("/rest/api/user/current"):
            return httpx.Response(200, json={"username": "doc-bot"})
        if path.endswith("/child/comment"):
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": "9",
                            "body": {"storage": {"value": "<p>hi</p>"}},
                            "version": {"number": 2},
                            "history": {"createdBy": {"username": "doc-bot"}},
                            "extensions": {
                                "location": "inline",
                                "inlineProperties": {"originalSelection": "hi"},
                                "resolution": {"status": "resolved"},
                            },
                        }
                    ]
                },
            )
        if path.endswith("/rest/api/content") and request.method == "POST":
            body = json.loads(request.content)
            if body.get("extensions", {}).get("location") == "inline":
                return httpx.Response(405, json={"message": "no"})
            return httpx.Response(
                200,
                json={
                    "id": "10",
                    "body": {"storage": {"value": body["body"]["storage"]["value"]}},
                    "version": {"number": 1},
                    "history": {"createdBy": {"username": "doc-bot"}},
                    "extensions": {"location": "footer"},
                },
            )
        if path.endswith("/rest/api/content/search"):
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "content": {
                                "id": "200",
                                "title": "Retry policy",
                                "version": {"number": 1},
                            },
                            "excerpt": "@@@hl@@@retries@@@endhl@@@ five",
                        }
                    ]
                },
            )
        if path.endswith("/rest/api/content/10") and request.method == "PUT":
            body = json.loads(request.content)
            assert body["version"]["number"] == 2
            return httpx.Response(
                200,
                json={
                    "id": "10",
                    "container": {"id": "100"},
                    "body": {"storage": {"value": "<p>edited</p>"}},
                    "version": {"number": 2},
                    "history": {"createdBy": {"username": "doc-bot"}},
                    "extensions": {"location": "footer"},
                },
            )
        if path.endswith("/rest/api/content/10") and request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(404)

    docs = Confluence(WIKI, "token", transport=httpx.MockTransport(handler))
    assert await docs.identity() == "doc-bot"
    comments = await docs.comments("100")
    assert comments[0].location == "inline" and comments[0].resolved
    assert comments[0].author == "doc-bot" and comments[0].selection == "hi"
    with pytest.raises(InlineUnsupported):
        await docs.post_inline_comment("100", "<p>x</p>", "hi")
    posted = await docs.post_comment("100", "<p>x</p>")
    assert posted.id == "10" and posted.location == "footer"
    hits = await docs.search("ENG", 'retry "policy"', limit=5)
    assert hits[0].page_id == "200" and hits[0].excerpt == "retries five"
    cql = seen[-1].url.params["cql"]
    assert cql == 'space = "ENG" AND type = page AND text ~ "retry \\"policy\\""'
    edited = await docs.edit_comment("10", "<p>edited</p>", 1)
    assert edited.version == 2 and edited.page_id == "100"
    assert await docs.delete_comment("10") is True
    assert all("Bearer token" in r.headers["Authorization"] for r in seen)
    await docs.close()


# -- corpus --------------------------------------------------------------------


def test_sections_follow_headings_and_keep_the_intro():
    sections = split_sections("100", SUBJECT_TEXT)
    assert [s.heading_path for s in sections] == [
        "(intro)",
        "Overview",
        "Overview > Limits",
        "Security",
    ]
    assert sections[2].id == "100#2"


async def test_corpus_collects_supporting_and_space_pages_and_searches_locally():
    docs = fake_docs()
    overrides = ReviewOverrides(check_space=True)
    corpus = await DocumentCorpus(SUBJECT).collect(
        docs, overrides, ProjectConfig(), FakeSecretScanner()
    )
    assert set(corpus.pages) == {"100", "200"}
    assert corpus.page("200").role == "space"
    assert corpus.inventory == [
        {
            "page_id": "200",
            "title": "Retry policy",
            "url": f"{WIKI}/pages/viewpage.action?pageId=200",
        }
    ]
    hits = corpus.search("retries charges")
    assert {h["page_id"] for h in hits} == {"100", "200"}
    assert corpus.section("100", "limits")["heading_path"] == "Overview > Limits"
    assert [u["id"] for u in corpus.units(8000)] == ["unit-1"]
    assert len(corpus.units(20)) == 4
    record = corpus.record()
    assert record["kind"] == "document" and "text" not in json.dumps(record)


async def test_corpus_refuses_a_page_that_moved_on():
    docs = fake_docs()
    with pytest.raises(LookupError):
        await DocumentCorpus(SUBJECT | {"version": 3}).collect(
            docs, ReviewOverrides(), ProjectConfig(), None
        )


async def test_corpus_scans_and_redacts_page_bodies():
    docs = fake_docs()
    docs.pages[PAGE_URL] = page(
        "100", "Payment API design", SUBJECT_TEXT + "\nkey: hunter2-secret\n", PAGE_URL
    )
    scanner = FakeSecretScanner([{"Secret": "hunter2-secret", "RuleID": "generic"}])
    corpus = await DocumentCorpus(SUBJECT).collect(
        docs, ReviewOverrides(), ProjectConfig(), scanner
    )
    text = "\n".join(s.text for s in corpus.sections_for("100"))
    assert "hunter2-secret" not in text and "[REDACTED:generic]" in text


# -- anchoring -----------------------------------------------------------------


def finding(**overrides):
    data = proposal(**overrides)
    return DocumentFinding(
        **data,
        id="f1",
        fingerprint="",
        stage="document_review",
        provenance=Provenance(
            agent="a", prompt_version="1", model="m", run_id="r", context_bundle_hash=""
        ),
    )


async def test_quotes_must_occur_on_the_page_and_headings_are_corrected():
    corpus = await DocumentCorpus(SUBJECT).collect(
        fake_docs(), ReviewOverrides(check_space=True), ProjectConfig(), None
    )
    ok = finding(
        anchor={"page_id": "100", "heading_path": "Wrong", "quote": "three times"}
    )
    result = policy.validate(ok, corpus)
    assert result.valid and ok.anchor.heading_path == "Overview"
    assert ok.related[0].heading_path == "Retry policy"
    missing = finding(
        anchor={"page_id": "100", "heading_path": "Overview", "quote": "never written"}
    )
    assert policy.validate(missing, corpus).valid is False
    off_page = finding(
        anchor={"page_id": "200", "heading_path": "Retry policy", "quote": "five times"}
    )
    assert "reviewed page" in policy.validate(off_page, corpus).reasons[0]
    lonely = finding(related=[])
    assert policy.validate(lonely, corpus).valid is False


def test_fingerprint_dedup_and_severity_policy():
    a = finding()
    b = finding(
        claim="Retry count contradicts the retry policy page!",
        severity_proposed="SUGGESTION",
    )
    for f in (a, b):
        f.fingerprint = policy.fingerprint(
            f.anchor.page_id, f.category, f.anchor.quote, f.claim
        )
    assert a.fingerprint == b.fingerprint
    assert policy.deduplicate([a, b]) == [a]
    c = finding(category="style", severity_proposed="NIT")
    assert policy.normalize(c).severity_final == "NIT"
    unverified = finding()
    unverified.status = "validated"
    assert policy.normalize(unverified).severity_final == "SUGGESTION"
    from reviewer.findings.models import VerificationResult

    verified = finding()
    verified.verification = VerificationResult(
        verdict="confirmed", counterargument="", reasoning=""
    )
    assert policy.normalize(verified).severity_final == "REQUIRED"
    blocker = finding(category="prompt_injection", severity_proposed="BLOCKER")
    blocker.verification = verified.verification
    assert policy.normalize(blocker).severity_final == "SUGGESTION"


# -- pipeline ------------------------------------------------------------------


def responses(verdict="confirmed"):
    return [
        envelope(
            "unit-1",
            [
                proposal(),
                proposal(
                    anchor={
                        "page_id": "100",
                        "heading_path": "Security",
                        "quote": "stored in plain text",
                    },
                    category="security",
                    claim="Tokens are stored unencrypted",
                    related=[],
                ),
            ],
        ),
        {"counterargument": "none", "reasoning": "matches", "verdict": verdict},
        {"counterargument": "none", "reasoning": "matches", "verdict": verdict},
    ]


async def run(store, tmp_path, docs, user_id, report_mode, llm=None, **extra):
    review = await admitted(
        store, user_id, report_mode=report_mode, check_space=True, **extra
    )
    llm = llm or FakeLLMClient(responses())
    pipeline = DocumentPipeline(
        store,
        personal(docs, user_id),
        settings(tmp_path),
        FakeSecretScanner(),
        llm_factory=lambda corpus, redactor: llm,
    )
    state = await pipeline.run(review.id)
    return await store.get(review.id), state, llm


async def test_pipeline_none_mode_stores_findings_and_report_without_writes(
    store, tmp_path
):
    user_id = await owner(store)
    docs = fake_docs()
    review, state, llm = await run(store, tmp_path, docs, user_id, "none")
    assert state == "PUBLISHED" and review.decision == "REQUEST_CHANGES"
    assert review.history[1:4] == [
        "CONTEXT_COLLECTION",
        "DOCUMENT_REVIEW",
        "EVIDENCE_VALIDATION",
    ]
    findings = await store.findings_for(review.id)
    assert {f["severity_final"] for f in findings} == {"REQUIRED"}
    assert {f["anchor"]["heading_path"] for f in findings} == {"Overview", "Security"}
    snapshot = await store.snapshot(review.id)
    assert snapshot["report"]["mode"] == "none"
    assert "Retry count contradicts" in snapshot["report"]["summary"]
    assert docs.writes == [] and await store.comments_for(review.id) == {}
    assert snapshot["bundle"]["documents"][1]["role"] == "space"
    # Both verifier calls saw claim and passages, never the proposer's reasoning.
    verifier = [c for c in llm.calls if c["stage"] == "document_verification"]
    assert len(verifier) == 2 and "Operators configure" not in verifier[0]["user"]
    stages = await store.stages(review.id)
    assert stages["document_review"].examined == ["unit-1"]


async def test_pipeline_draft_mode_holds_comments_locally(store, tmp_path):
    user_id = await owner(store)
    docs = fake_docs()
    review, state, _ = await run(store, tmp_path, docs, user_id, "draft")
    rows = await store.comments_for(review.id)
    assert docs.writes == []
    assert {r["status"] for r in rows.values()} == {"drafted"}
    assert "summary" in rows and len(rows) == 3
    assert (await store.snapshot(review.id))["report"]["mode"] == "draft"


async def test_pipeline_applied_mode_posts_marked_comments_once(store, tmp_path):
    user_id = await owner(store)
    docs = fake_docs()
    review, state, _ = await run(store, tmp_path, docs, user_id, "applied")
    comments = await docs.comments("100")
    assert len(comments) == 3 and all(c.location == "footer" for c in comments)
    assert all(
        f"<!-- ai-review:owner={user_id} -->" in c.body_storage for c in comments
    )
    assert sum("ai-review:summary=100@4" in c.body_storage for c in comments) == 1
    assert "＠" not in comments[0].body_storage or "@" not in SUBJECT_TEXT
    rows = await store.comments_for(review.id)
    assert {r["status"] for r in rows.values()} == {"committed"}
    # A rerun reconciles by fingerprint instead of posting again.
    review2, state2, _ = await run(
        store, tmp_path, docs, user_id, "applied", event_id="evt-doc-2"
    )
    assert len(await docs.comments("100")) == 3
    assert {r["status"] for r in (await store.comments_for(review2.id)).values()} == {
        "committed"
    }


async def test_pipeline_inline_attempt_falls_back_to_footer(store, tmp_path):
    user_id = await owner(store)
    docs = fake_docs()
    docs.inline_supported = False
    config_path = tmp_path / "projects.json"
    config_path.write_text(
        json.dumps({"defaults": {"document_review": {"inline_comments": True}}})
    )
    review = await admitted(store, user_id, report_mode="applied")
    pipeline = DocumentPipeline(
        store,
        personal(docs, user_id),
        settings(tmp_path, config_path=config_path),
        FakeSecretScanner(),
        llm_factory=lambda corpus, redactor: FakeLLMClient(responses()),
    )
    await pipeline.run(review.id)
    # Without the space, the consistency claim has no counterpart and is dropped:
    # one security comment plus the summary, both footer after the 4xx.
    posted = await docs.comments("100")
    assert len(posted) == 2 and all(c.location == "footer" for c in posted)
    docs.inline_supported = True
    review2 = await admitted(store, user_id, report_mode="applied", event_id="e2")
    await pipeline.run(review2.id)
    # Already posted findings are reconciled, not posted again inline.
    assert len(await docs.comments("100")) == 2
    findings = await store.findings_for(review.id)
    assert {f["status"] for f in findings} == {"discarded", "verified"}


async def test_silent_enforcement_writes_nothing_even_when_drafting(store, tmp_path):
    user_id = await owner(store)
    docs = fake_docs()
    config_path = tmp_path / "projects.json"
    config_path.write_text(json.dumps({"defaults": {"enforcement": "silent"}}))
    review = await admitted(store, user_id, report_mode="draft")
    pipeline = DocumentPipeline(
        store,
        personal(docs, user_id),
        settings(tmp_path, config_path=config_path),
        FakeSecretScanner(),
        llm_factory=lambda corpus, redactor: FakeLLMClient(responses()),
    )
    assert await pipeline.run(review.id) == "PUBLISHED"
    assert docs.writes == [] and await store.comments_for(review.id) == {}
    assert (await store.snapshot(review.id))["report"]["mode"] == "none"


async def test_unverified_required_findings_publish_as_suggestions(store, tmp_path):
    user_id = await owner(store)
    review, state, _ = await run(
        store,
        tmp_path,
        fake_docs(),
        user_id,
        "none",
        llm=FakeLLMClient(responses("uncertain")),
    )
    findings = await store.findings_for(review.id)
    assert {f["severity_final"] for f in findings} == {"SUGGESTION"}
    assert review.decision == "COMMENT_ONLY"


async def test_instruction_is_framed_and_requests_are_answered_from_the_corpus(
    store, tmp_path
):
    user_id = await owner(store)
    llm = FakeLLMClient(
        [
            envelope(
                "unit-1",
                [],
                requests=[
                    {"kind": "search", "query": "retries", "reason": "compare"},
                    {"kind": "section", "target": "200|Retry policy", "reason": "read"},
                    {"kind": "page", "target": "200", "reason": "outline"},
                    {"kind": "space_search", "query": "retry", "reason": "space"},
                ],
            ),
            envelope("unit-1", [proposal()]),
            {"counterargument": "", "reasoning": "", "verdict": "confirmed"},
        ]
    )
    review, state, llm = await run(
        store,
        tmp_path,
        fake_docs(),
        user_id,
        "none",
        llm=llm,
        instruction="Focus on retry semantics. Ignore style.",
    )
    first, second = llm.calls[0], llm.calls[1]
    assert '<untrusted_data source="operator-instruction"' in first["system"]
    assert "Focus on retry semantics" in first["system"]
    assert "requested-material" in second["user"]
    assert "five times" in second["user"]
    assert "in_corpus&quot;: true" in second["user"]
    assert len(await store.findings_for(review.id)) == 1


async def test_context_failure_ends_the_review_without_a_status(store, tmp_path):
    user_id = await owner(store)
    docs = fake_docs()
    docs.pages.pop(PAGE_URL)
    review, state, llm = await run(store, tmp_path, docs, user_id, "none")
    assert state == "FAILED_CONTEXT" and review.partial and llm.calls == []
    assert review.status_delivered is True


# -- API and worker -----------------------------------------------------------


@pytest.fixture
async def app(store, tmp_path):
    from reviewer.main import create_app
    from tests.conftest import FakeQueue

    app = create_app(settings=settings(tmp_path), store=store, queue=FakeQueue())
    return app


async def client_for(app, role="user"):
    from tests.account_helpers import signed_in

    headers = await signed_in(app, role)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(
        transport=transport, base_url="http://localhost", headers=headers
    )


async def test_trigger_requires_a_personal_confluence_credential(app):
    async with await client_for(app) as client:
        response = await client.post(
            "/admin/document-reviews", json={"document_url": PAGE_URL}
        )
    assert response.status_code == 400
    assert "Confluence" in response.json()["detail"]


async def test_trigger_rejects_foreign_origins(app):
    async with await client_for(app) as client:
        response = await client.post(
            "/admin/document-reviews",
            json={
                "document_url": "https://evil.example/pages/viewpage.action?pageId=1"
            },
        )
    assert response.status_code == 400


async def test_trigger_records_the_subject_and_the_worker_admits_it(app, store):
    docs = fake_docs()
    app.state.personal_docs_factory = lambda *_: docs
    async with await client_for(app) as client:
        account = app.state.test_account
        await Credentials(store, app.state.settings).save(
            account.id, "confluence", "conf-token"
        )
        response = await client.post(
            "/admin/document-reviews",
            json={
                "document_url": PAGE_URL,
                "supporting_urls": [f"{WIKI}/pages/viewpage.action?pageId=200"],
                "instruction": "Check retry semantics",
                "check_space": True,
                "report_mode": "draft",
                "models": {"document_review": "google/gemini-3.8-flash"},
            },
        )
        assert response.status_code == 202, response.text
        body = response.json()
        assert body["kind"] == "document" and body["subject"]["version"] == 4
        name, args = app.state.queue.jobs[-1]
        assert name == "receive_event" and args[0]["kind"] == "document"
        assert args[0]["overrides"]["instruction"] == "Check retry semantics"
        assert args[0]["overrides"]["check_space"] is True
        # Unknown roles for a document run are refused.
        bad = await client.post(
            "/admin/document-reviews",
            json={"document_url": PAGE_URL, "models": {"line_review": "x"}},
        )
        assert bad.status_code == 400
        # The worker admits it with the owner's pinned credentials.
        from reviewer.worker import receive_event
        from tests.conftest import FakeQueue

        ctx = {
            "store": store,
            "settings": app.state.settings,
            "redis": FakeQueue(),
            "personal_docs_factory": lambda *_: docs,
        }
        await receive_event(ctx, args[0])
        review = await store.by_event(args[0]["event_id"])
        assert review is not None and review.kind == "document"
        assert review.owner_user_id == account.id and review.principal_id == "doc-bot"
        assert ctx["redis"].jobs == [("run_review", (review.id,))]
        polled = await client.get(f"/admin/reviews?event_id={args[0]['event_id']}")
        assert polled.status_code == 200
        assert polled.json()["kind"] == "document"
        assert polled.json()["subject"]["page_id"] == "100"
        assert polled.json()["missing_integrations"] == []
        listed = await client.get("/admin/reviews")
        assert listed.json()["reviews"][0]["kind"] == "document"
        # Redelivery of an admitted trigger does nothing.
        await receive_event(ctx, args[0])
        assert len(ctx["redis"].jobs) == 1
        recheck = await client.post(f"/admin/reviews/{review.id}/recheck")
        assert recheck.status_code == 409


async def test_worker_rejects_a_trigger_whose_identity_changed(app, store):
    docs = fake_docs()
    app.state.personal_docs_factory = lambda *_: docs
    async with await client_for(app) as client:
        account = app.state.test_account
        await Credentials(store, app.state.settings).save(
            account.id, "confluence", "conf-token"
        )
        response = await client.post(
            "/admin/document-reviews", json={"document_url": PAGE_URL}
        )
        assert response.status_code == 202
        payload = app.state.queue.jobs[-1][1][0]
    from reviewer.worker import receive_event
    from tests.conftest import FakeQueue

    docs.user = "someone-else"
    ctx = {
        "store": store,
        "settings": app.state.settings,
        "redis": FakeQueue(),
        "personal_docs_factory": lambda *_: docs,
    }
    await receive_event(ctx, payload)
    async with store.sessions() as session:
        assert (
            await session.get(ReviewTrigger, payload["event_id"])
        ).state == "REJECTED"
    assert ctx["redis"].jobs == []


# -- comment controls ----------------------------------------------------------


async def test_drafted_rows_publish_edit_and_remove_through_personal_docs(
    store, tmp_path
):
    user_id = await owner(store)
    docs = fake_docs()
    review, state, _ = await run(store, tmp_path, docs, user_id, "draft")
    comments = DocumentComments(store, personal(docs, user_id), review, ProjectConfig())
    view = await comments.sync()
    assert {r["status"] for r in view} == {"drafted"}
    key = next(r["key"] for r in view if r["key"] != "summary")
    assert await comments.act(key, "publish") == "succeeded"
    posted = await docs.comments("100")
    assert (
        len(posted) == 1
        and f"<!-- ai-review:owner={user_id} -->" in posted[0].body_storage
    )
    assert comments.rows[key]["status"] == "committed"
    # The API re-syncs after every action; the revision follows the remote body.
    revision = next(r["revision"] for r in await comments.sync() if r["key"] == key)
    assert (
        await comments.act(key, "edit", revision, "Reworded by the owner")
        == "succeeded"
    )
    body = (await docs.comments("100"))[0].body_storage
    assert "Reworded by the owner" in body and f"fingerprint={key}" in body
    assert "AI review" in body
    with pytest.raises(Exception):
        await comments.act(key, "resolve")
    assert await comments.act(key, "remove") == "succeeded"
    assert await docs.comments("100") == []
    assert comments.rows[key]["status"] == "removed"
    # Publishing the summary posts it too; a drafted summary is a real comment.
    assert await comments.act("summary", "publish") == "succeeded"
    assert "ai-review:summary=100@4" in (await docs.comments("100"))[0].body_storage


async def test_writes_refuse_a_page_that_changed_since_the_review(store, tmp_path):
    from reviewer.services.forge.gitlab import StaleReview

    user_id = await owner(store)
    docs = fake_docs()
    review, state, _ = await run(store, tmp_path, docs, user_id, "draft")
    docs.pages[PAGE_URL] = page(
        "100", "Payment API design", SUBJECT_TEXT, PAGE_URL, version=5
    )
    comments = DocumentComments(store, personal(docs, user_id), review, ProjectConfig())
    await comments.sync()
    key = next(k for k in comments.rows if k != "summary")
    with pytest.raises(StaleReview):
        await comments.act(key, "publish")
    assert docs.writes == []


async def test_personal_docs_only_edits_its_own_comments(store, tmp_path):
    from reviewer.accounts.credentials import CredentialUnavailable

    user_id = await owner(store)
    docs = fake_docs()
    foreign = await docs.post_comment("100", "<p>someone else</p>")
    client = personal(docs, user_id)
    with pytest.raises(CredentialUnavailable):
        await client.edit_comment(foreign.id, "<p>x</p>", 1)
    with pytest.raises(CredentialUnavailable):
        await client.delete_comment(foreign.id)
    docs.user = "impostor"
    with pytest.raises(CredentialUnavailable):
        await client.post_comment("100", "<p>x</p>")


# -- chat ----------------------------------------------------------------------


async def test_document_chat_reads_sections_and_validates_citations(store, tmp_path):
    from reviewer.agents.chat import answer
    from reviewer.config.models import resolve
    from reviewer.context.document_chat_context import DocumentChatSources

    user_id = await owner(store)
    docs = fake_docs()
    review, state, _ = await run(store, tmp_path, docs, user_id, "none")
    snapshot = await store.snapshot(review.id)
    sources = DocumentChatSources(
        store, review, snapshot, ProjectConfig(), docs=docs, scanner=FakeSecretScanner()
    )
    record = await sources.fixed()
    assert record["review"]["kind"] == "document"
    assert record["subject"]["page_id"] == "100"
    assert record["findings"][0]["quote"]
    assert "100" in sources.given()["page"] and "200" in sources.given()["page"]
    llm = FakeLLMClient(
        [
            {
                "context_requests": [
                    {"kind": "section", "target": "100|Security", "reason": "read"},
                    {"kind": "search", "target": "retries", "reason": "compare"},
                    {"kind": "file", "target": "svc.py", "reason": "n/a"},
                ],
                "answer": None,
                "citations": [],
            },
            {
                "answer": "Tokens are stored in plain text.",
                "citations": [
                    {"kind": "page", "ref": "100#Security"},
                    {"kind": "page", "ref": "999"},
                    {"kind": "finding", "ref": record["findings"][0]["id"]},
                ],
            },
        ]
    )
    spec = resolve(settings(tmp_path))["chat"]
    text, citations, used, model = await answer(
        sources, "What does it say about tokens?", llm, ProjectConfig(), review.id, spec
    )
    assert text.startswith("Tokens")
    assert [c["ref"] for c in citations] == [
        "100#Security",
        record["findings"][0]["id"],
    ]
    assert "section:100|Security" in used and "search:retries" in used
    assert "not available for a document review" in llm.calls[1]["user"]
    assert "plain text in the database" in llm.calls[1]["user"]
