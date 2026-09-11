"""Per-role model selection: resolution order, limits and what may be chosen."""

import pytest

from reviewer.config.loader import load_project
from reviewer.config.models import assignment, catalog, resolve
from reviewer.config.schema import (
    DEFAULT_ROLE_MODELS,
    ROLES,
    ModelProfile,
    ModelSpec,
    ProjectConfig,
    Settings,
)
from reviewer.context.models import ReviewOverrides

GATEWAY = dict(gateway_base_url="https://gateway.internal/v1")


def settings(**kwargs):
    # Pinned rather than inherited: Settings reads .env, and a developer's own
    # model configuration must not decide whether these cases pass.
    return Settings(
        **GATEWAY
        | dict(
            model_strong="",
            model_fast="",
            model_verifier="",
            model_roles={},
            model_limits={},
            model_catalog=[],
            model_context_tokens=32000,
        )
        | kwargs
    )


def test_every_role_resolves_to_a_model_by_default():
    resolved = resolve(settings())
    assert set(ROLES) <= set(resolved)
    assert assignment(resolved) == DEFAULT_ROLE_MODELS
    # Stages that were one tier before are separately selectable now.
    assert resolved["correctness"].model == resolved["complexity"].model
    assert resolved["tests_"].model == "google/gemini-3.8-flash"
    assert resolved["verification"].model != resolved["line_review"].model


def test_selection_layers_from_most_specific_to_least():
    config = ProjectConfig(
        models=ModelProfile(
            default=ModelSpec(model="project/default"),
            roles={"correctness": ModelSpec(model="project/correctness")},
        )
    )
    resolved = resolve(
        settings(model_roles={"design": "env/design"}),
        config,
        ReviewOverrides(models={"correctness": "run/correctness"}),
    )
    # The run's own choice beats the project, which beats the environment, which
    # beats the shipped default.
    assert resolved["correctness"].model == "run/correctness"
    assert resolved["design"].model == "project/default"
    assert resolved["purpose"].model == "project/default"
    # With no project profile at all the environment is what answers.
    plain = resolve(settings(model_roles={"design": "env/design"}))
    assert plain["design"].model == "env/design"
    assert plain["purpose"].model == DEFAULT_ROLE_MODELS["purpose"]


def test_limits_come_from_the_model_not_the_installation():
    resolved = resolve(
        settings(
            model_roles={"purpose": "vendor/small"},
            model_limits={
                "vendor/small": {"context_tokens": 8000, "max_output_tokens": 900}
            },
        )
    )
    assert resolved["purpose"].context_tokens == 8000
    assert resolved["purpose"].max_output_tokens == 900
    # A model with no declared limits falls back to the installation-wide window.
    assert resolved["design"].context_tokens == 32000
    assert resolved["design"].max_output_tokens is None
    # A role's own spec outranks the per-model table.
    config = ProjectConfig(
        models=ModelProfile(
            roles={"purpose": ModelSpec(model="vendor/small", context_tokens=12000)}
        )
    )
    assert resolve(settings(), config)["purpose"].context_tokens == 12000


def test_legacy_tiers_still_resolve_for_callers_that_ask_for_them():
    resolved = resolve(
        settings(model_strong="approved/strong", model_fast="approved/fast")
    )
    assert resolved["strong"].model == "approved/strong"
    assert resolved["fast"].model == "approved/fast"
    # "verification" is a role as well as a legacy tier name; the role wins, so
    # the verifier cannot be silently moved by legacy configuration.
    assert resolved["verification"].model == DEFAULT_ROLE_MODELS["verification"]
    # A tier is something a caller may still ask for, not part of the review:
    # reporting one as the model a role ran on would overstate what ran.
    assert set(assignment(resolved)) == set(ROLES)


def test_catalog_defaults_to_the_models_this_installation_uses():
    assert catalog(settings()) == sorted(set(DEFAULT_ROLE_MODELS.values()))
    # Legacy tiers are not selectable: no role runs on them.
    assert "approved/strong" not in catalog(settings(model_strong="approved/strong"))
    assert catalog(settings(model_catalog=["b/two", "a/one", "a/one"])) == [
        "a/one",
        "b/two",
    ]


def test_repository_configuration_cannot_choose_a_model(tmp_path):
    path = tmp_path / "projects.json"
    path.write_text('{"defaults": {}, "projects": {}}')
    with pytest.raises(ValueError):
        load_project(path, 7, "models:\n  default:\n    model: attacker/model\n")


def test_a_profile_rejects_roles_that_do_not_exist():
    with pytest.raises(ValueError):
        ModelProfile(roles={"not_a_stage": ModelSpec(model="a/b")})
