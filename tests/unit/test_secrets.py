from reviewer.config.schema import StaticTool
from reviewer.context.models import ChangedFile, DiffLine
from reviewer.context.redaction import Redactor
from reviewer.services.secrets.scanner import SecretScanner
from reviewer.services.static.runner import StaticRunner


async def test_scanner_preserves_line_and_redacts_all_prompt_values(tmp_path):
    binary = tmp_path / "gitleaks"
    binary.write_text("""#!/usr/bin/env python3
import json,sys
from pathlib import Path
report=Path(sys.argv[sys.argv.index('--report-path')+1])
report.write_text(json.dumps([{'Secret':'planted_credential_123','RuleID':'fixture','File':'file-0.txt','StartLine':12,'EndLine':12}]))
sys.exit(1)
""")
    binary.chmod(0o755)
    matches = await SecretScanner(str(binary)).scan(
        [
            ChangedFile(
                path="a.py",
                change_type="added",
                lines=[
                    DiffLine(
                        text="planted_credential_123",
                        new_line=12,
                        old_line=None,
                        kind="added",
                    )
                ],
            )
        ]
    )
    assert matches[0]["File"] == "a.py" and matches[0]["StartLine"] == 12
    redactor = Redactor(matches)
    assert "planted_credential_123" not in str(
        redactor.object(
            {"prompt": "planted_credential_123", "other": ["planted_credential_123"]}
        )
    )


def test_static_sandbox_has_no_network_or_privileges(tmp_path):
    tool = StaticTool(name="lint", image="lint@sha256:" + "a" * 64, command=["lint"])
    argv = StaticRunner().argv(tool, tmp_path, "test")
    assert argv[argv.index("--network") + 1] == "none"
    assert "--read-only" in argv and "--cap-drop" in argv
    assert "readonly" in argv[argv.index("--mount") + 1]


async def test_real_gitleaks_when_installed():
    import secrets
    import shutil
    import string

    import pytest

    if not shutil.which("gitleaks"):
        pytest.skip("gitleaks is included in the Docker test image")
    token = "ghp_" + "".join(
        secrets.choice(string.ascii_letters + string.digits) for _ in range(36)
    )
    matches = await SecretScanner().scan(
        [
            ChangedFile(
                path="config.py",
                change_type="added",
                lines=[
                    DiffLine(
                        text=f'token = "{token}"',
                        new_line=1,
                        old_line=None,
                        kind="added",
                    )
                ],
            )
        ]
    )
    assert matches and token not in Redactor(matches).text(token)
