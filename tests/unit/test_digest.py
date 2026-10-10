from test_stages import bundle

from reviewer.agents.base import DefectReviewAgent
from reviewer.context.digest import condense, terms
from reviewer.context.partition import partition
from reviewer.services.docs.confluence import DocumentContext
from reviewer.services.issues.jira import AcceptanceCriterion, IssueContext


def page(text, page_id="1"):
    return DocumentContext(
        page_id=page_id,
        space="S",
        title="Spec",
        url="https://w/p",
        version=1,
        text_md=text,
    )


def spec():
    filler = "Unrelated narrative about onboarding and branding. " * 40
    return "\n".join(
        [
            "Intro: this page specifies the payments platform.",
            "# Refund rules\nRefunds over the ledger limit require approval. "
            + filler[:200],
            "# Marketing copy\n" + filler,
            "# Office locations\n" + filler,
        ]
    )


def payment_bundle(tmp_path):
    b = bundle(tmp_path, 1)
    b.code.files[0].path = "billing/refund_ledger.py"
    b.code.files[0].lines[0].text = "def approve_refund(ledger, amount): ..."
    b.issue = IssueContext(
        key="PAY-1",
        type="Story",
        summary="Refunds need approval",
        description_md="As finance I want refund approval.",
        acceptance_criteria=[
            AcceptanceCriterion(id="AC1", text="Refunds over the limit need approval")
        ],
        status="Open",
    )
    b.documents = [page(spec())]
    return b


def test_small_requirements_are_sent_whole(tmp_path):
    b = payment_bundle(tmp_path)
    requirements, omitted = condense(b, 100000)
    assert not omitted
    assert requirements["documents"][0]["text_md"] == spec()


def test_large_pages_keep_their_opening_and_the_sections_about_the_change(tmp_path):
    b = payment_bundle(tmp_path)
    requirements, omitted = condense(b, 200)
    assert omitted
    (document,) = requirements["documents"]
    assert document["text_md"].startswith("Intro: this page specifies")
    assert "# Refund rules" in document["text_md"]
    assert "# Marketing copy" not in document["text_md"]
    assert document["omitted_sections"] == ["Marketing copy", "Office locations"]
    # The issue and its acceptance criteria are never condensed.
    assert requirements["issue"]["acceptance_criteria"][0]["id"] == "AC1"


def test_prompt_condenses_once_per_review_and_records_it(tmp_path):
    b = payment_bundle(tmp_path)
    agent = DefectReviewAgent(document_tokens=200)
    _, user = agent.build_prompt(b, partition(b)[0])
    assert "Office locations" in user  # listed as omitted, so the model knows
    assert "# Marketing copy" not in user
    assert b.degradations == ["requirements_condensed"]
    assert len(agent.condensed) == 1


def test_terms_split_identifiers_and_drop_noise():
    assert terms("approveRefund ledger_limit the of") == [
        "approve",
        "refund",
        "ledger",
        "limit",
    ]
