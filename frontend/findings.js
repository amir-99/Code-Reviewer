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
