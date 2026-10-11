/* Small DOM and formatting helpers every part of the dashboard shares. */

export const $ = id => document.getElementById(id);
export const all = selector => Array.from(document.querySelectorAll(selector));
export const el = (tag, text, cls) => {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = text;
  if (cls) node.className = cls;
  return node;
};

export const short = (sha, length = 8) => String(sha ?? '').slice(0, length);

export function ago(value, now = Date.now()) {
  const at = Date.parse(value ?? '');
  if (!Number.isFinite(at)) return '—';
  const seconds = Math.max(0, (now - at) / 1000);
  if (seconds < 60) return `${Math.floor(seconds)}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

export function span(milliseconds) {
  const seconds = Math.max(0, Math.round(milliseconds / 1000));
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
  return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
}

export function notice(message, kind = '') {
  const toast = el('div', message, `toast ${kind}`.trim());
  $('toasts').append(toast);
  while ($('toasts').children.length > 3) $('toasts').firstChild.remove();
  setTimeout(() => toast.remove(), 8000);
}

export const delay = (ms, signal) => new Promise(resolve => {
  const done = () => { clearTimeout(timer); signal.removeEventListener('abort', done); resolve(); };
  const timer = setTimeout(done, ms); signal.addEventListener('abort', done, {once: true});
  if (signal.aborted) done();
});

// Native <dialog> keeps this dependency-free; resolves false on Cancel, backdrop click cancel, or Escape.
export function confirmDialog(title, message, confirmLabel) {
  return new Promise(resolve => {
    const dialog = document.createElement('dialog'); dialog.className = 'confirm-dialog';
    dialog.append(el('h3', title), el('p', message));
    const actions = el('div', null, 'form-actions');
    const cancel = el('button', 'Cancel'); cancel.type = 'button';
    const confirmButton = el('button', confirmLabel, 'danger'); confirmButton.type = 'button';
    actions.append(cancel, confirmButton);
    dialog.append(actions);
    document.body.append(dialog);
    let result = false;
    cancel.onclick = () => dialog.close();
    confirmButton.onclick = () => { result = true; dialog.close(); };
    dialog.addEventListener('close', () => { dialog.remove(); resolve(result); }, {once: true});
    dialog.showModal();
  });
}
