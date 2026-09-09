"""Which model each role runs on, resolved once per review.

Selection is layered, most specific first: the operator overrides supplied with
a manual run, the project's own profile, the installation's role map, and the
shipped defaults. Resolution happens once, at the start of a run, so a
configuration edit mid-review cannot split one review across two models; the
resolved map is persisted with the review and reported in its activity.
"""

from dataclasses import dataclass

from reviewer.config.schema import (
    DEFAULT_ROLE_MODELS,
    LEGACY_TIERS,
    ROLES,
    ModelSpec,
)


@dataclass(frozen=True)
class ResolvedModel:
    role: str
    model: str
    context_tokens: int
    max_output_tokens: int | None = None
    reasoning_effort: str | None = None


def _spec(source):
    """The ModelSpec a single configuration layer supplies for one role."""
    if source is None:
        return None
    if isinstance(source, str):
        return ModelSpec(model=source) if source else None
    return source


def _limits(settings, model):
    return settings.model_limits.get(model, {})


def resolve(settings, config=None, overrides=None):
    """Role -> ResolvedModel for every role, plus any configured legacy tier.

    A role with no model anywhere is absent from the map rather than carrying an
    empty ID: the gateway fails that stage explicitly instead of posting a
    request naming no model.
    """
    profile = getattr(config, "models", None)
    chosen = dict(getattr(overrides, "models", None) or {})
    resolved = {}
    for role in ROLES:
        spec = (
            _spec(chosen.get(role))
            or _spec(getattr(profile, "roles", {}).get(role))
            or _spec(getattr(profile, "default", None))
            or _spec(settings.model_roles.get(role))
            or _spec(DEFAULT_ROLE_MODELS.get(role))
        )
        if spec is not None:
            resolved[role] = _resolved(settings, role, spec)
    for tier, attribute in LEGACY_TIERS.items():
        # "verification" is both a legacy tier name and a role; the role wins.
        if tier not in resolved and getattr(settings, attribute, ""):
            resolved[tier] = _resolved(
                settings, tier, ModelSpec(model=getattr(settings, attribute))
            )
    return resolved


def _resolved(settings, role, spec: ModelSpec):
    limits = _limits(settings, spec.model)
    return ResolvedModel(
        role=role,
        model=spec.model,
        context_tokens=(
            spec.context_tokens
            or limits.get("context_tokens")
            or settings.model_context_tokens
        ),
        max_output_tokens=spec.max_output_tokens or limits.get("max_output_tokens"),
        reasoning_effort=spec.reasoning_effort,
    )


def assignment(resolved):
    """The role -> model ID map recorded on the budget and shown to operators.

    Only real roles: a legacy tier is something a caller may still ask for, not
    a part of the review, and reporting one would overstate what ran.
    """
    return {role: resolved[role].model for role in sorted(ROLES) if role in resolved}


def catalog(settings, config=None):
    """Model IDs an operator may select for a manual run.

    Defaults to the models this installation already uses, so an operator can
    never select an ID the deployment has not been configured for.
    """
    if settings.model_catalog:
        return sorted(dict.fromkeys(settings.model_catalog))
    resolved = resolve(settings, config)
    # Legacy tiers are excluded: no role runs on them, so offering their models
    # would let an operator select one this deployment never exercises.
    return sorted(
        {resolved[role].model for role in ROLES if role in resolved}
        | set(DEFAULT_ROLE_MODELS.values())
    )
