/* Account panels: a user's profile and integrations, an admin's user list,
   and account activation. Mounted once; reads the signed-in account live. */
import {$, confirmDialog, el, notice} from './dom.js';

export async function readiness(request) {
  try {
    const statuses = await (await request('/profile/integrations')).json();
    const missing = Object.entries(statuses).filter(([,v]) => v.status !== 'configured').map(([k]) => k);
    $('credential-readiness').textContent = missing.length ? `Missing or invalid: ${missing.join(', ')}. Gateway and GitLab are required; missing Jira or Confluence degrades requirements.` : 'Integration credentials are configured.';
  } catch (error) { notice(error.message, 'error'); }
}

export function mountAccounts({request, account: currentAccount, clearSession}) {
  function field(form, label, type='text') {
    const input = el('input'); input.type = type; input.required = true;
    const wrapper = el('label', label); wrapper.append(input); form.append(wrapper); return input;
  }
  function action(label, fn) {
    const button = el('button', label); button.type = 'button';
    button.onclick = async () => { button.disabled = true; try { await fn(); } catch (error) { notice(error.message, 'error'); } finally { button.disabled = false; } };
    return button;
  }
  function jsonRequest(path, method, body) { return request(path, {method, headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)}); }
  function panel(title) { $('account-panel').hidden = false; $('review-layout').hidden = true; $('account-title').textContent = title; $('account-content').replaceChildren(); return $('account-content'); }
  function initials(name) {
    const parts = String(name || '').trim().split(/\s+/).filter(Boolean);
    return parts.length ? (parts[0][0] + (parts[1]?.[0] || '')).toUpperCase() : '?';
  }
  function statusBadge(status) {
    const tone = status === 'configured' ? 'tone-good' : status === 'invalid' ? 'tone-bad' : 'tone-warn';
    return el('span', status, `badge ${tone}`);
  }
  $('nav-profile').onclick = async () => {
    const admin = currentAccount().role === 'admin';
    const root = panel(admin ? 'Account' : 'Profile');
    root.append(el('p', `Signed in as ${currentAccount().display_name} (@${currentAccount().login}).`, 'account-lede'));

    const passwordCard = el('section', null, 'card');
    passwordCard.append(el('h3', 'Change password'));
    const password = el('form'); const grid = el('div', null, 'form-grid');
    const old = field(grid, 'Current password', 'password');
    const next = field(grid, 'New password', 'password'); next.minLength = 12;
    password.append(grid);
    const passwordActions = el('div', null, 'form-actions');
    const submit = el('button', 'Change password', 'primary'); submit.type = 'submit';
    passwordActions.append(submit, el('span', 'At least 12 characters. You will be signed out everywhere after this.', 'hint'));
    password.append(passwordActions);
    password.onsubmit = async event => { event.preventDefault(); try { await jsonRequest('/auth/password', 'POST', {current_password:old.value, password:next.value}); clearSession(); notice('Password changed. Sign in again.'); } catch (error) { notice(error.message, 'error'); } finally { old.value = ''; next.value = ''; } };
    passwordCard.append(password);
    root.append(passwordCard);
    if (admin) return;

    root.append(el('h3', 'Integrations'));
    root.append(el('p', 'Personal credentials used whenever you trigger a review. Gateway and GitLab are required for merge requests; Jira and Confluence unlock requirement linkage. Document reviews need Gateway and Confluence.', 'hint'));
    try {
      const statuses = await (await request('/profile/integrations')).json();
      for (const [name, state] of Object.entries(statuses)) {
        const card = el('section', null, 'card');
        const head = el('div', null, 'row');
        head.append(el('h3', name), statusBadge(state.status));
        card.append(head);
        const form = el('form'); const input = field(form, 'Replacement token', 'password'); input.autocomplete = 'off';
        const save = el('button', 'Save token', 'primary'); save.type = 'submit'; form.append(save);
        form.onsubmit = async event => { event.preventDefault(); const value = input.value; input.value = ''; try { await jsonRequest(`/profile/integrations/${name}`, 'PUT', {token:value}); notice('Token saved'); $('nav-profile').click(); readiness(request); } catch (error) { notice(error.message, 'error'); } };
        card.append(form);
        const cardActions = el('div', null, 'user-actions');
        cardActions.append(action('Remove', async () => { await request(`/profile/integrations/${name}`, {method:'DELETE'}); $('nav-profile').click(); readiness(request); }), action('Check connection', async () => { const result = await (await request(`/profile/integrations/${name}/check`, {method:'POST'})).json(); notice(result.reason || result.status); }));
        card.append(cardActions);
        root.append(card);
      }
    } catch (error) { notice(error.message, 'error'); }
  };
  $('activate').onsubmit = async event => { event.preventDefault(); try { await jsonRequest('/auth/activate', 'POST', {token:$('activation-token').value, password:$('activation-password').value}); notice('Password set. You can sign in.'); } catch (error) { notice(error.message, 'error'); } finally { $('activation-token').value = ''; $('activation-password').value = ''; } };
  $('nav-users').onclick = async () => {
    const root = panel('Users');
    root.append(el('p', 'Create accounts and manage roles, access and removal for everyone on this deployment.', 'account-lede'));

    const createCard = el('section', null, 'card');
    createCard.append(el('h3', 'Create account'));
    const form = el('form'); const grid = el('div', null, 'form-grid');
    const login = field(grid, 'Login'); const name = field(grid, 'Display name');
    const roleLabel = el('label', 'Role'); const role = el('select');
    for (const value of ['user','admin']) { const option = el('option', value); option.value = value; role.append(option); }
    roleLabel.append(role); grid.append(roleLabel);
    form.append(grid);
    const createActions = el('div', null, 'form-actions');
    const submit = el('button', 'Create account', 'primary'); submit.type = 'submit';
    createActions.append(submit, el('span', "They'll get a one-time activation link to set their own password.", 'hint'));
    form.append(createActions);
    const delivery = el('div', null, 'token-callout'); delivery.hidden = true;
    createCard.append(form, delivery);
    root.append(createCard);
    form.onsubmit = async event => {
      event.preventDefault();
      try {
        const result = await (await jsonRequest('/auth/users','POST',{login:login.value,display_name:name.value,role:role.value})).json();
        delivery.replaceChildren(el('span', 'Deliver privately, expires in one hour:'), el('code', result.activation_token));
        const copy = el('button', 'Copy'); copy.type = 'button';
        copy.onclick = async () => { try { await navigator.clipboard.writeText(result.activation_token); notice('Copied to clipboard'); } catch { notice('Could not copy — select and copy the token manually', 'error'); } };
        delivery.append(copy);
        delivery.hidden = false;
        form.reset();
        await listUsers();
      } catch (error) { notice(error.message,'error'); }
    };

    const listCard = el('section', null, 'card');
    listCard.append(el('h3', 'Accounts'));
    const searchWrap = el('div', null, 'user-search');
    const search = el('input'); search.type = 'search'; search.placeholder = 'Search by login…'; search.setAttribute('aria-label','Search accounts');
    searchWrap.append(search);
    listCard.append(searchWrap);
    const showRemovedRow = el('label', null, 'checkbox-row');
    const showRemoved = el('input'); showRemoved.type = 'checkbox';
    showRemovedRow.append(showRemoved, el('span', 'Show removed accounts'));
    listCard.append(showRemovedRow);
    const list = el('div', null, 'user-list');
    listCard.append(list);
    root.append(listCard);

    function userRow(user) {
      const removed = Boolean(user.removed_at);
      const row = el('section', null, `card user-row${removed ? ' is-removed' : ''}`);
      row.append(el('div', initials(user.display_name), `avatar${user.role === 'admin' ? ' role-admin' : ''}`));
      const main = el('div', null, 'user-main');
      const heading = el('div', null, 'row');
      heading.append(el('h3', user.display_name), el('span', user.role, `badge${user.role === 'admin' ? ' tone-info' : ''}`));
      heading.append(removed ? el('span', 'removed', 'badge tone-bad') : el('span', user.active ? 'active' : 'disabled', `badge ${user.active ? 'tone-good' : 'tone-warn'}`));
      main.append(heading, el('p', `@${user.login}`, 'user-meta'));
      const actionsRow = el('div', null, 'user-actions');
      if (!removed) {
        for (const [label, body] of [['Toggle role',{action:'role',value:user.role === 'admin' ? 'user':'admin'}], [user.active ? 'Disable':'Reactivate',{action:'active',value:!user.active}], ['Revoke sessions',{action:'revoke_sessions'}], ['Start recovery (revokes integrations)',{action:'recovery'}]]) actionsRow.append(action(label, async () => { const result = await (await jsonRequest(`/auth/users/${user.id}`,'POST',body)).json(); if (result.activation_token) notice(`Deliver privately: ${result.activation_token}`); await listUsers(); }));
        const remove = action('Delete user', async () => {
          const ok = await confirmDialog('Delete this account?', `${user.display_name} (@${user.login}) will be signed out everywhere, permanently lose their saved integration credentials, and won't be able to sign back in. This can't be undone.`, 'Delete user');
          if (!ok) return;
          await jsonRequest(`/auth/users/${user.id}`, 'POST', {action:'remove'});
          notice('Account removed');
          await listUsers();
        });
        remove.className = 'danger';
        if (user.id === currentAccount().id) { remove.disabled = true; remove.title = "You can't delete the account you're signed in as."; }
        actionsRow.append(remove);
      }
      main.append(actionsRow);
      row.append(main);
      return row;
    }

    async function listUsers() {
      try {
        const result = await (await request(`/auth/users?search=${encodeURIComponent(search.value)}`)).json();
        const users = result.users.filter(user => showRemoved.checked || !user.removed_at);
        list.replaceChildren();
        if (!users.length) { list.append(el('p', result.users.length ? 'No matching accounts.' : 'No accounts yet.', 'empty-line')); return; }
        for (const user of users) list.append(userRow(user));
      } catch (error) { notice(error.message,'error'); }
    }
    search.oninput = listUsers; showRemoved.onchange = listUsers; await listUsers();
  };
}
