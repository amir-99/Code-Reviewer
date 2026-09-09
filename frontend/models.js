/* Model selection, as the interface sees it.

   The review picks a model per role rather than per tier, so two questions come
   up all over the dashboard: which role does this activity belong to, and which
   model served it. Both are answered here, away from the DOM, so they can be
   tested directly. */

// What each role is called in the interface. The pipeline names roles after the
// stage that makes the call; "tests_" keeps Python's trailing underscore, which
// is not something to show an operator.
export const ROLE_LABELS = {
  purpose: 'Purpose', design: 'Design', correctness: 'Correctness', complexity: 'Complexity',
  tests_: 'Tests', line_review: 'Line review', system_context: 'System context',
  verification: 'Verification', recheck: 'Recheck',
};

// Tools that call a model of their own rather than inheriting a stage's.
export const TOOL_ROLES = {'Independent verifier': 'verification', 'Fix recheck': 'recheck'};

export const roleLabel = role => ROLE_LABELS[role] || String(role ?? '');

/* The role an activity's calls resolve through. A stage is its own role, the
   verifier and the recheck judge have theirs, and anything nested inside one of
   those — a work unit, the gateway call itself — inherits it. */
export function roleFor(kind, data = {}, ancestor = null) {
  if (kind === 'agent') return String(data.name ?? '');
  if (kind === 'llm_attempt') return String(data.role || data.stage || '');
  if (kind === 'state' || kind === 'models' || kind === 'run') return '';
  return TOOL_ROLES[String(data.name ?? '')] || ancestor?.role || '';
}

/* What a row reports as its model: for a gateway attempt the model that
   actually served it, and otherwise the model this review resolved for the
   row's role. An attempt is the ground truth — it is the only event that knows
   a call was retried on a different model than the one selection announced. */
export function modelFor(kind, data = {}, role = '', resolved = {}) {
  if (kind === 'llm_attempt' && data.model) return String(data.model);
  return (role && resolved[role]) || '';
}

/* The body a manual run sends: only the roles the operator actually moved.
   A run that names nothing is exactly a webhook run, so an unchanged picker
   must not pin the defaults into the review's overrides — that would freeze
   today's configuration into a replay of tomorrow's. */
export function chosenModels(choices) {
  const entries = [...(choices instanceof Map ? choices : Object.entries(choices ?? {}))]
    .filter(([role, model]) => role && model);
  return Object.fromEntries(entries);
}
