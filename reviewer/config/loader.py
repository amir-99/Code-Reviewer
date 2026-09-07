import json
from pathlib import Path

import yaml

from reviewer.config.schema import ProjectConfig


def merge(base, override):
    result = dict(base)
    for key, value in override.items():
        result[key] = (
            merge(result.get(key, {}), value) if isinstance(value, dict) else value
        )
    return result


def load_project(path: Path, project_id: int, repository_yaml: str | None = None):
    data = json.loads(path.read_text()) if path.exists() else {}
    resolved = merge(
        data.get("defaults", {}), data.get("projects", {}).get(str(project_id), {})
    )
    if repository_yaml:
        override = yaml.safe_load(repository_yaml) or {}
        # Repo code cannot choose commands, images, enforcement, network or model.
        if set(override) - {"review", "issue_tracker", "documents", "languages"}:
            raise ValueError("Repository config contains operator-only settings")
        resolved = merge(resolved, override)
    return ProjectConfig.model_validate(resolved)
