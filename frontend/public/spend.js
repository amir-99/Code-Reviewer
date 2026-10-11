/* What a review is costing, as the interface accounts for it.

   Two sources answer the same question at different times. The API reports the
   audited spend of every call the review has already written down, current as
   of the snapshot's event cursor. The activity stream then reports each gateway
   attempt as it happens. Adding only the attempts newer than that cursor keeps
   a running total that is live without double counting the replay a reconnect
   sends, and every snapshot re-bases it on the audited numbers. */

export const emptySpend = () => ({
  roles: [], calls: 0, retries: 0, tokens_in: 0, tokens_out: 0, tokens: 0,
  cost: 0, unpriced_calls: 0, token_ceiling: null,
});

const key = row => `${row.role ?? ''}\u0000${row.model ?? ''}`;

/* One gateway attempt, added to a running delta. Every attempt counts, whether
   or not it succeeded: a stage that burned its budget on retries spent it. */
export function addAttempt(delta, data = {}) {
  const tokens_in = Number(data.tokens_in) || 0;
  const tokens_out = Number(data.tokens_out) || 0;
  // A model with no configured price did not cost nothing, so it is counted in
  // tokens and reported as unpriced rather than folded into the total as zero.
  const priced = typeof data.cost === 'number';
  const row = delta.roles.find(r => key(r) === key({role: data.role || data.stage, model: data.model}))
    ?? addRow(delta, {role: data.role || data.stage || '', model: data.model || ''});
  const failed = data.outcome && data.outcome !== 'success' ? 1 : 0;
  for (const target of [delta, row]) {
    target.calls += 1;
    target.retries += failed;
    target.tokens_in += tokens_in;
    target.tokens_out += tokens_out;
    target.tokens += tokens_in + tokens_out;
    target.cost += priced ? data.cost : 0;
    target.unpriced_calls += priced ? 0 : 1;
  }
  return delta;
}

function addRow(spend, {role, model}) {
  const row = {role, model, calls: 0, retries: 0, tokens_in: 0, tokens_out: 0,
    tokens: 0, cost: 0, unpriced_calls: 0};
  spend.roles.push(row);
  return row;
}

/* The audited spend plus everything seen since it was taken, most expensive
   first — and by tokens where nothing has a price yet, so the table still ranks
   when the installation has configured none. */
export function mergeSpend(audited, delta) {
  const merged = {...emptySpend(), ...(audited || {}), roles: []};
  const rows = new Map();
  for (const row of [...(audited?.roles || []), ...(delta?.roles || [])]) {
    const found = rows.get(key(row));
    if (!found) rows.set(key(row), {...row});
    else for (const field of ['calls', 'retries', 'tokens_in', 'tokens_out', 'tokens', 'cost', 'unpriced_calls']) {
      found[field] = (found[field] || 0) + (row[field] || 0);
    }
  }
  merged.roles = [...rows.values()].sort((a, b) => (b.cost - a.cost) || (b.tokens - a.tokens));
  for (const field of ['calls', 'retries', 'tokens_in', 'tokens_out', 'tokens', 'cost', 'unpriced_calls']) {
    merged[field] = (audited?.[field] || 0) + (delta?.[field] || 0);
  }
  return merged;
}

export function tokens(value) {
  const count = Number(value) || 0;
  if (count < 1000) return String(count);
  if (count < 1000000) return `${(count / 1000).toFixed(count < 10000 ? 1 : 0)}k`;
  return `${(count / 1000000).toFixed(2)}M`;
}

/* Cost is shown to the cent only once it is worth a cent; below that the extra
   digits are the difference between "free" and "nearly free", which is the
   whole question when a stage runs a few hundred times a day. */
export function cost(value, {unpriced = 0} = {}) {
  if (typeof value !== 'number' || Number.isNaN(value)) return unpriced ? 'unpriced' : '—';
  const shown = value >= 1 ? `$${value.toFixed(2)}`
    : value >= 0.01 ? `$${value.toFixed(3)}`
    : value > 0 ? `$${value.toFixed(4)}`
    : unpriced ? 'unpriced' : '$0.0000';
  // A total that omits calls must say so: a review whose prices are half
  // configured is not a review that half cost nothing.
  return unpriced && value > 0 ? `≥ ${shown}` : shown;
}

/* How much of the review's token ceiling has been spent. The ceiling is what
   the budget actually enforces, so this is a limit, not a forecast. */
export function usage(spend) {
  const ceiling = Number(spend?.token_ceiling) || 0;
  if (!ceiling) return null;
  return {ceiling, used: spend.tokens, percent: Math.min(100, (spend.tokens / ceiling) * 100)};
}


// Collapse the audited role/model pairs along either comparison dimension.
export function breakdown(spend, dimension) {
  const rows = new Map();
  for (const row of spend.roles || []) {
    const label = row[dimension] || 'Unknown';
    const total = rows.get(label) || {label, calls: 0, retries: 0, tokens_in: 0,
      tokens_out: 0, tokens: 0, cost: 0, unpriced_calls: 0};
    for (const field of ['calls', 'retries', 'tokens_in', 'tokens_out', 'tokens', 'cost', 'unpriced_calls']) {
      total[field] += row[field] || 0;
    }
    rows.set(label, total);
  }
  return [...rows.values()].sort((a, b) => b.cost - a.cost || b.tokens - a.tokens);
}
