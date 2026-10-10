// Why a finding has no verdict when the pipeline chose not to verify it, as
// opposed to a verifier that never answered.
const SKIPPED = {
  static_tool: ['not verified: static tool', 'A static analyzer covers this category, so no verifier ran.'],
  outside_change: ['not verified: outside the change', 'The finding is not on code this merge request touches, so no verifier ran.'],
  existing_limit: ['not verified: beyond FYI limit', 'Only two pre-existing findings are shown; the rest are suppressed without verification.'],
  evidence_invalid: ['not verified: invalid evidence', 'Its cited evidence failed mechanical validation, so no verifier ran.'],
  not_required: ['verification not required', 'Its category and severity do not call for independent verification.'],
};

export function verificationBadge(finding) {
  if (finding.verdict) {
    return {text: finding.verdict, tone: finding.verdict === 'confirmed' ? 'tone-good' : 'tone-warn', title: ''};
  }
  const skipped = SKIPPED[finding.unverified_reason];
  if (skipped) return {text: skipped[0], tone: '', title: skipped[1]};
  return {text: 'unverified', tone: 'tone-warn', title: 'Verification did not run or did not answer.'};
}

// Both filters apply independently; old findings have unknown impact.
export function matchesFinding(finding, disposition, impact) {
  return (disposition === 'all' || finding.severity === disposition)
    && (impact === 'all' || (finding.impact_level || 'unknown') === impact);
}

export function commentActions(comment, canManage) {
  if (!canManage || !comment || comment.conflict) return [];
  if (comment.intent === 'remove') return ['remove'];
  const actions = [];
  if (['drafted', 'committed'].includes(comment.status)) actions.push('edit', 'remove');
  if (comment.status === 'committed' && comment.thread_status === 'open') actions.push('resolve');
  return actions;
}

export function bulkCommentKeys(comments, action) {
  return comments.filter(c => !c.conflict && !c.intent && c.eligible !== false && (action === 'resolve_all'
    ? c.status === 'committed' && c.thread_status === 'open'
    : c.status === 'drafted' || (c.status === 'not_published' && c.body && (c.key === 'summary' || c.position))))
    .map(c => c.key);
}
