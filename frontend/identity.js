export function identity(review) {
  if (review.kind === 'document') {
    const subject = review.subject || {};
    return `${subject.space ? `${subject.space} · ` : ''}${subject.title || `Page ${subject.page_id ?? ''}`}`;
  }
  return `${review.project_path || review.project_name || `Project ${review.project_id}`} · !${review.mr_iid}`;
}

export function identityNode(review, document) {
  const node = document.createElement('span');
  if (review.kind === 'document' || !(review.project_path || review.project_name)) {
    node.textContent = identity(review);
    return node;
  }
  node.textContent = review.project_path || review.project_name;
  const id = document.createElement('span');
  id.className = 'muted';
  id.textContent = ` (${review.project_id})`;
  node.append(id, ` · !${review.mr_iid}`);
  return node;
}
