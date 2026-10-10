from types import SimpleNamespace

from test_findings import finding
from test_stages import bundle

from reviewer.agents.severity_triage import SeverityTriageAgent, TriageResult
from reviewer.config.schema import ProjectConfig
from reviewer.findings.models import Severity, VerificationResult


def candidate(severity="REQUIRED", stage="defect_review", verified=False):
    f = finding()
    f.stage = stage
    f.severity_final = severity
    f.anchor.introduced_by_this_change = True
    if verified:
        f.verification = VerificationResult(
            counterargument="", reasoning="holds", verdict="confirmed"
        )
    return f


class Judge:
    def __init__(self, verdicts):
        self.verdicts = verdicts
        self.calls = []
        self.specs = {"severity_triage": SimpleNamespace(context_tokens=100000)}

    async def complete(self, **kwargs):
        self.calls.append(kwargs)
        return TriageResult.model_validate(
            {
                "judgements": [
                    {
                        "finding": key,
                        "severity": value,
                        "impact_level": "LOW",
                        "reason": "r",
                    }
                    for key, value in self.verdicts.items()
                ]
            }
        )


async def test_triage_lowers_but_never_raises(tmp_path):
    lowered, kept, suggestion = candidate(), candidate(), candidate("SUGGESTION")
    judge = Judge({"f1": "NIT", "f2": "keep", "f3": "SUGGESTION"})
    await SeverityTriageAgent().triage(
        [lowered, kept, suggestion], bundle(tmp_path, 1), judge, ProjectConfig()
    )
    assert lowered.severity_final == Severity.NIT and lowered.triage.applied
    assert lowered.impact_level == "LOW"
    assert kept.severity_final == Severity.REQUIRED and not kept.triage.applied
    assert suggestion.severity_final == Severity.SUGGESTION
    assert not suggestion.triage.applied


async def test_gating_findings_cannot_be_argued_down(tmp_path):
    gating = candidate(verified=True)
    config = ProjectConfig(enforcement="gating")
    await SeverityTriageAgent().triage(
        [gating], bundle(tmp_path, 1), Judge({"f1": "FYI"}), config
    )
    # Recorded for the audit, not applied: the check stays deterministic.
    assert gating.severity_final == Severity.REQUIRED
    assert gating.triage.proposed == Severity.FYI and not gating.triage.applied
    advisory = candidate(verified=True)
    await SeverityTriageAgent().triage(
        [advisory], bundle(tmp_path, 1), Judge({"f1": "FYI"}), ProjectConfig()
    )
    assert advisory.severity_final == Severity.FYI


async def test_secrets_and_unconfigured_triage_are_left_alone(tmp_path):
    secret = candidate(stage="secrets")
    judge = Judge({"f1": "NIT"})
    await SeverityTriageAgent().triage(
        [secret], bundle(tmp_path, 1), judge, ProjectConfig()
    )
    assert not judge.calls and secret.severity_final == Severity.REQUIRED
    judge.specs = {}
    other = candidate()
    await SeverityTriageAgent().triage(
        [other], bundle(tmp_path, 1), judge, ProjectConfig()
    )
    assert not judge.calls and other.triage is None
