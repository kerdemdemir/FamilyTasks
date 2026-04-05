'use strict';

// ---------------------------------------------------------------------------
// Auth
// ---------------------------------------------------------------------------

const DAYS = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"];

function getToken() { return localStorage.getItem('parent_token'); }
function isParent()  { return !!getToken(); }

function applyRole() {
  const parent = isParent();
  document.getElementById('lock-btn').textContent   = parent ? '🔓' : '🔒';
  document.getElementById('role-label').textContent = parent ? 'Parent mode' : 'Viewer mode';
  document.querySelectorAll('.parent-only').forEach(el => {
    el.style.display = parent ? '' : 'none';
  });
}

function toggleAuth() {
  if (isParent()) {
    localStorage.removeItem('parent_token');
    applyRole();
    toast('Logged out');
    loadDashboard();
  } else {
    document.getElementById('auth-password').value = '';
    document.getElementById('auth-error').style.display = 'none';
    document.getElementById('auth-modal').style.display = 'flex';
    setTimeout(() => document.getElementById('auth-password').focus(), 80);
  }
}

function closeAuth() {
  document.getElementById('auth-modal').style.display = 'none';
}

async function submitAuth() {
  const pw  = document.getElementById('auth-password').value;
  const res = await fetch('/api/auth', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ password: pw }),
  });
  if (res.ok) {
    const data = await res.json();
    localStorage.setItem('parent_token', data.token);
    closeAuth();
    applyRole();
    toast('Welcome, Parent! 👋', 'success');
    loadDashboard();
  } else {
    document.getElementById('auth-error').style.display = 'block';
    document.getElementById('auth-password').select();
  }
}

// Attach auth header to all write requests
function authHeaders() {
  const h = { 'Content-Type': 'application/json' };
  const t = getToken();
  if (t) h['Authorization'] = `Bearer ${t}`;
  return h;
}


// ---------------------------------------------------------------------------
// Data
// ---------------------------------------------------------------------------

let dash = null;

async function loadDashboard() {
  try {
    const res = await fetch('/api/dashboard');
    dash = await res.json();
    render(dash);
  } catch (e) { console.error(e); }
}

function fmt(n) { return `${parseFloat(n).toFixed(0)} DKK`; }


// ---------------------------------------------------------------------------
// Render
// ---------------------------------------------------------------------------

function render(d) {
  document.getElementById('child-name').textContent = d.settings.child_name || 'Ela';
  document.getElementById('balance').textContent    = fmt(d.balance);

  const goalAmount = parseFloat(d.settings.goal_amount || 1200);
  document.getElementById('goal-name').textContent  = '🎯 ' + (d.settings.goal_name || 'Goal');
  document.getElementById('goal-text').textContent  = `${fmt(d.balance)} / ${fmt(goalAmount)}`;
  document.getElementById('goal-fill').style.width  = `${Math.min(d.goal_progress, 100)}%`;

  renderStrikes(d.strikes);
  renderMandatory(d.mandatory);
  renderOptional('optional-tasks', d.optional_tasks);
  renderTransactions('tx-list', d.transactions);
  checkWeeklyPurchase(d.transactions);
  startCountdowns();
  applyRole();
}

// ---------------------------------------------------------------------------
// Countdown timers
// ---------------------------------------------------------------------------

let _countdownTimer = null;

function deadlineDate(deadlineHour, deadlineWeekday, frequency) {
  const now      = new Date();
  const deadline = new Date();
  deadline.setHours(deadlineHour, 0, 0, 0);

  if (frequency === 'weekly') {
    // Server uses Python weekday (Mon=0); JS getDay() uses Sun=0
    const targetJsDay  = (parseInt(deadlineWeekday) + 1) % 7;
    const currentJsDay = now.getDay();
    const daysUntil    = (targetJsDay - currentJsDay + 7) % 7;
    deadline.setDate(now.getDate() + daysUntil);
  }
  return deadline;
}

function fmtCountdown(ms) {
  if (ms <= 0) return null;
  const s   = Math.floor(ms / 1000);
  const h   = Math.floor(s / 3600);
  const m   = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  return `${h}h ${String(m).padStart(2,'0')}m ${String(sec).padStart(2,'0')}s`;
}

function updateCountdowns() {
  document.querySelectorAll('.countdown').forEach(el => {
    const dl = deadlineDate(
      parseInt(el.dataset.hour),
      parseInt(el.dataset.weekday),
      el.dataset.freq
    );
    const ms  = dl - Date.now();
    const txt = fmtCountdown(ms);
    if (txt) {
      el.textContent = `⏱ ${txt} left`;
      el.className   = ms < 3_600_000 ? 'countdown countdown-urgent' : 'countdown';
    } else {
      el.textContent = "⌛ Time's up!";
      el.className   = 'countdown countdown-urgent';
    }
  });
}

function startCountdowns() {
  if (_countdownTimer) clearInterval(_countdownTimer);
  updateCountdowns();
  _countdownTimer = setInterval(updateCountdowns, 1000);
}

// ── Strikes card ────────────────────────────────────────────────────────────
function renderStrikes(st) {
  const el   = document.getElementById('strikes-content');
  const card = document.getElementById('strikes-card');
  const { count, max, punishment } = st;
  const hit  = count >= max;

  // Dot indicators
  const dots = Array.from({ length: max }, (_, i) =>
    `<span class="strike-dot ${i < count ? 'filled' : ''}"></span>`
  ).join('');

  const parentControls = isParent() ? `
    <div class="strike-controls">
      <button class="btn btn-sm btn-secondary" onclick="changeStrike('remove')" ${count <= 0 ? 'disabled' : ''}>− Remove</button>
      <button class="btn btn-sm btn-danger"    onclick="changeStrike('add')"    ${count >= max ? 'disabled' : ''}>+ Add Strike</button>
      <button class="btn btn-sm btn-secondary" onclick="changeStrike('reset')"  ${count === 0 ? 'disabled' : ''}>Reset</button>
    </div>` : '';

  el.innerHTML = `
    <div class="strike-dots">${dots}</div>
    <div class="strike-count">${count} / ${max} strikes</div>
    <div class="strike-punishment ${hit ? 'punishment-hit' : ''}">
      ${hit ? '🚨' : '⚠️'} Punishment: <strong>${punishment || '—'}</strong>
    </div>
    ${parentControls}`;

  card.style.borderLeft = hit ? '4px solid var(--danger)' : count > 0 ? '4px solid var(--warning)' : '4px solid var(--border)';
}

async function changeStrike(action) {
  const res  = await fetch(`/api/strikes/${action}`, { method: 'POST', headers: authHeaders() });
  const data = await res.json();
  if (!res.ok) { toast(data.detail || 'Error', 'error'); return; }
  if (action === 'add') {
    const hit = data.count >= data.max;
    toast(hit ? `🚨 Max strikes reached!` : `⚡ Strike ${data.count}/${data.max} added — WhatsApp sent`, hit ? 'error' : 'success');
  } else if (action === 'remove') {
    toast(`Strike removed — now ${data.count}/${data.max}`);
  } else {
    toast('All strikes cleared ✅', 'success');
  }
  loadDashboard();
}

// ── Mandatory tasks card ────────────────────────────────────────────────────
function renderMandatory(m) {
  const tasksEl   = document.getElementById('mandatory-tasks');
  const summaryEl = document.getElementById('screen-time-summary');
  const card      = document.getElementById('mandatory-card');

  if (!m.tasks.length) {
    tasksEl.innerHTML = '<div class="empty">No mandatory tasks</div>';
    summaryEl.innerHTML = '';
    return;
  }

  tasksEl.innerHTML = m.tasks.map(t => {
    const statusIcon = t.status === 'done'    ? '<span class="badge badge-done">✅ Done</span>'
                     : t.status === 'missed'  ? '<span class="badge badge-missed">❌ Missed</span>'
                     :                          '<span class="badge badge-pending">⏳ Pending</span>';

    const checkbox = isParent()
      ? `<div class="task-check ${t.done ? 'done' : ''}"
              onclick="${t.done ? `undo(${t.id})` : `complete(${t.id})`}"
              title="${t.done ? 'Undo' : 'Mark done'}"></div>`
      : `<div class="task-check ${t.done ? 'done' : ''} readonly"></div>`;

    const countdownHtml = t.status === 'pending'
      ? `<span class="countdown"
              data-freq="${t.frequency}"
              data-hour="${t.deadline_hour}"
              data-weekday="${t.deadline_weekday}">calculating…</span>`
      : '';

    return `
      <div class="task-row">
        ${checkbox}
        <div class="task-info">
          <div class="task-name ${t.done ? 'striked' : ''}">${t.name}</div>
          <div class="task-due">${t.due_label}</div>
          ${countdownHtml}
        </div>
        <div style="display:flex;align-items:center;gap:8px;flex-shrink:0">
          <div class="task-reward">+${t.reward_dkk} DKK</div>
          ${statusIcon}
        </div>
      </div>`;
  }).join('');

  // Summary / screen-time panel
  const penalty = m.total_penalty_minutes;
  if (penalty === 0) {
    const allDone = m.tasks.every(t => t.status === 'done');
    if (allDone) {
      summaryEl.innerHTML = `<div class="st-good">✅ All done — no screen time restrictions today!</div>`;
      card.style.borderLeft = '4px solid var(--success)';
    } else {
      summaryEl.innerHTML = `<div class="st-pending">⏳ Tasks still pending — no penalty yet</div>`;
      card.style.borderLeft = '4px solid var(--warning)';
    }
  } else {
    summaryEl.innerHTML = `
      <div class="family-link-box">
        <strong>📲 Reduce screen time by ${penalty} min in Family Link</strong>
        ${m.tasks.filter(t=>t.status==='missed').map(t=>`<div class="penalty-row">– ${t.name} (−${m.total_penalty_minutes/m.tasks.filter(x=>x.status==='missed').length} min)</div>`).join('')}
      </div>`;
    card.style.borderLeft = '4px solid var(--danger)';
  }
}

// ── Optional tasks ──────────────────────────────────────────────────────────
function renderOptional(id, tasks) {
  const el = document.getElementById(id);
  if (!tasks.length) { el.innerHTML = '<div class="empty">No optional tasks</div>'; return; }
  el.innerHTML = tasks.map(t => `
    <div class="task-row">
      <div class="task-info">
        <div class="task-name">${t.name}</div>
      </div>
      <div class="task-reward">+${t.reward_dkk} DKK</div>
      ${isParent() ? `<button class="log-btn" onclick="complete(${t.id})">Done!</button>` : ''}
    </div>`).join('');
}

// ── Transactions ────────────────────────────────────────────────────────────
function renderTransactions(id, txs) {
  const el = document.getElementById(id);
  if (!txs.length) { el.innerHTML = '<div class="empty">No transactions yet</div>'; return; }
  el.innerHTML = txs.map(t => {
    const pos  = t.amount >= 0;
    const when = new Date(t.created_at).toLocaleDateString('da-DK', {
      day:'numeric', month:'short', hour:'2-digit', minute:'2-digit'
    });
    return `
      <div class="tx-row">
        <div><div class="tx-desc">${t.description}</div><div class="tx-date">${when}</div></div>
        <div class="tx-amount ${pos?'pos':'neg'}">${pos?'+':''}${parseFloat(t.amount).toFixed(0)} DKK</div>
      </div>`;
  }).join('');
}


// ---------------------------------------------------------------------------
// Actions (parent only)
// ---------------------------------------------------------------------------

async function complete(taskId) {
  const res  = await fetch(`/api/complete/${taskId}`, { method:'POST', headers: authHeaders() });
  const data = await res.json();
  if (!res.ok) { toast(data.detail||'Error','error'); return; }
  toast(`+${data.earned} DKK earned! 🎉`, 'success');
  loadDashboard();
}

async function undo(taskId) {
  if (!confirm('Undo this completion?')) return;
  const res  = await fetch(`/api/complete/${taskId}`, { method:'DELETE', headers: authHeaders() });
  const data = await res.json();
  if (!res.ok) { toast(data.detail||'Error','error'); return; }
  toast('Completion removed');
  loadDashboard();
}

function getWeekStart() {
  const now = new Date();
  const day = now.getDay(); // 0=Sun
  const monday = new Date(now);
  monday.setDate(now.getDate() - (day === 0 ? 6 : day - 1));
  monday.setHours(0, 0, 0, 0);
  return monday;
}

function checkWeeklyPurchase(transactions) {
  const monday  = getWeekStart();
  const bought  = transactions.find(t =>
    t.amount < 0 &&
    t.description.startsWith('Spent:') &&
    new Date(t.created_at) >= monday
  );
  const el = document.getElementById('weekly-buy-warning');
  if (el) el.style.display = bought ? 'block' : 'none';
  return bought || null;
}

async function setBalance() {
  const target = parseFloat(document.getElementById('set-balance-amount').value);
  if (isNaN(target) || target < 0) { toast('Enter a valid amount', 'error'); return; }
  const current = dash ? dash.balance : 0;
  const diff = target - current;
  if (diff === 0) { toast('Balance is already ' + fmt(target)); return; }
  const desc = diff > 0
    ? `Balance adjusted +${diff.toFixed(0)} DKK (set to ${target.toFixed(0)})`
    : `Balance adjusted −${Math.abs(diff).toFixed(0)} DKK (set to ${target.toFixed(0)})`;
  const res = await fetch('/api/transactions', {
    method: 'POST', headers: authHeaders(),
    body: JSON.stringify({ amount: diff, description: desc }),
  });
  const data = await res.json();
  if (!res.ok) { toast(data.detail || 'Error', 'error'); return; }
  toast(`Balance set to ${fmt(target)}`, 'success');
  document.getElementById('set-balance-amount').value = '';
  loadDashboard();
}

async function addFunds() {
  const amount = parseFloat(document.getElementById('spend-amount').value);
  const desc   = document.getElementById('spend-desc').value.trim();
  if (!amount || amount <= 0) { toast('Enter a valid amount', 'error'); return; }
  if (!desc)                  { toast('Enter a description', 'error'); return; }
  const res = await fetch('/api/transactions', {
    method: 'POST', headers: authHeaders(),
    body: JSON.stringify({ amount: amount, description: `Added: ${desc}` }),
  });
  const data = await res.json();
  if (!res.ok) { toast(data.detail || 'Error', 'error'); return; }
  toast(`+${amount} DKK added`, 'success');
  document.getElementById('spend-amount').value = '';
  document.getElementById('spend-desc').value   = '';
  loadDashboard();
}

async function recordSpending() {
  const amount = parseFloat(document.getElementById('spend-amount').value);
  const desc   = document.getElementById('spend-desc').value.trim();
  if (!amount || amount <= 0) { toast('Enter a valid amount','error'); return; }
  if (!desc)                  { toast('Enter a description','error'); return; }
  if (dash && checkWeeklyPurchase(dash.transactions)) {
    toast('You already bought something this week! 🚫', 'error');
    return;
  }
  const res = await fetch('/api/transactions', {
    method:'POST', headers: authHeaders(),
    body: JSON.stringify({ amount: -amount, description: `Spent: ${desc}` }),
  });
  const data = await res.json();
  if (!res.ok) { toast(data.detail||'Error','error'); return; }
  toast(`−${amount} DKK recorded`, 'success');
  document.getElementById('spend-amount').value = '';
  document.getElementById('spend-desc').value   = '';
  loadDashboard();
}


// ---------------------------------------------------------------------------
// Settings modal
// ---------------------------------------------------------------------------

async function openSettings() {
  if (!dash) return;
  const s = dash.settings;
  document.getElementById('s-site-url').value         = s.site_url                || '';
  document.getElementById('s-strike-max').value       = s.strike_max              || 3;
  document.getElementById('s-strike-punishment').value= s.strike_punishment       || '';
  document.getElementById('s-child-name').value = s.child_name              || '';
  document.getElementById('s-goal-name').value  = s.goal_name               || '';
  document.getElementById('s-goal-amt').value   = s.goal_amount             || '';
  document.getElementById('s-penalty').value    = s.screen_time_penalty_min || 30;
  await refreshContacts();
  await refreshTaskList();
  document.getElementById('settings-modal').style.display = 'flex';
}

function closeSettings() {
  document.getElementById('settings-modal').style.display = 'none';
}

async function saveSettings() {
  const body = {
    child_name:              document.getElementById('s-child-name').value,
    goal_name:               document.getElementById('s-goal-name').value,
    goal_amount:             parseFloat(document.getElementById('s-goal-amt').value),
    screen_time_penalty_min: parseInt(document.getElementById('s-penalty').value),
    site_url:                document.getElementById('s-site-url').value.trim(),
  };
  // Save strikes config separately
  await fetch('/api/strikes', {
    method: 'PUT', headers: authHeaders(),
    body: JSON.stringify({
      max:        parseInt(document.getElementById('s-strike-max').value),
      punishment: document.getElementById('s-strike-punishment').value.trim(),
    }),
  });
  const res = await fetch('/api/settings', {
    method:'PUT', headers: authHeaders(), body: JSON.stringify(body),
  });
  if (res.ok) { toast('Settings saved!','success'); closeSettings(); loadDashboard(); }
}

async function refreshContacts() {
  const res      = await fetch('/api/whatsapp/contacts');
  const contacts = await res.json();
  const el       = document.getElementById('wa-contacts-list');
  if (!contacts.length) {
    el.innerHTML = '<div class="wa-empty">No contacts yet</div>';
    return;
  }
  el.innerHTML = contacts.map(c => `
    <div class="wa-contact-row">
      <span class="wa-contact-label">${c.label}</span>
      <span class="wa-contact-phone">${c.phone}</span>
      <button class="btn btn-sm btn-danger" onclick="deleteContact(${c.id})">Remove</button>
    </div>`).join('');
}

async function addContact() {
  const label  = document.getElementById('wa-new-label').value.trim();
  const phone  = document.getElementById('wa-new-phone').value.trim();
  const apikey = document.getElementById('wa-new-apikey').value.trim();
  if (!label || !phone || !apikey) { toast('Fill in all fields', 'error'); return; }
  const res = await fetch('/api/whatsapp/contacts', {
    method: 'POST', headers: authHeaders(),
    body: JSON.stringify({ label, phone, apikey }),
  });
  if (res.ok) {
    document.getElementById('wa-new-label').value  = '';
    document.getElementById('wa-new-phone').value  = '';
    document.getElementById('wa-new-apikey').value = '';
    toast(`${label} added!`, 'success');
    refreshContacts();
  } else {
    const d = await res.json();
    toast(d.detail || 'Failed', 'error');
  }
}

async function deleteContact(id) {
  if (!confirm('Remove this contact?')) return;
  await fetch(`/api/whatsapp/contacts/${id}`, { method: 'DELETE', headers: authHeaders() });
  refreshContacts();
}

async function testWhatsApp() {
  const res  = await fetch('/api/whatsapp/test', { method: 'POST', headers: authHeaders() });
  const data = await res.json();
  if (!res.ok) { toast(data.detail || 'Failed', 'error'); return; }
  const failed = data.results.filter(r => !r.ok);
  if (failed.length === 0) toast('Sent to all contacts! 📲', 'success');
  else toast(`Failed for: ${failed.map(r=>r.label).join(', ')}`, 'error');
}

function renderTaskRow(t) {
  const isMandatory = t.type === 'mandatory';
  const deadlineInfo = isMandatory
    ? (t.frequency === 'weekly'
        ? `Deadline: ${DAYS[t.deadline_weekday||4]} at ${t.deadline_hour||20}:00`
        : `Deadline: daily by ${t.deadline_hour||20}:00`)
    : '';

  return `
    <div class="settings-task" id="stask-${t.id}">
      <div class="st-info">
        <div class="st-name">${t.name}</div>
        <div class="st-meta">${t.frequency} · ${t.reward_dkk} DKK
          ${deadlineInfo ? ` · <span class="deadline-chip">${deadlineInfo}</span>` : ''}
        </div>
      </div>
      <div style="display:flex;gap:6px;flex-shrink:0">
        ${isMandatory ? `<button class="btn btn-sm btn-secondary" onclick="editDeadline(${t.id})">Deadline</button>` : ''}
        <button class="btn btn-sm ${t.is_active?'btn-secondary':'btn-primary'}"
                onclick="toggleTask(${t.id},${!t.is_active})">
          ${t.is_active?'Disable':'Enable'}
        </button>
        <button class="btn btn-sm btn-danger" onclick="deleteTask(${t.id})">Delete</button>
      </div>
    </div>
    <div class="deadline-edit" id="dedit-${t.id}" style="display:none">
      <div class="deadline-edit-inner">
        <label>Deadline hour (0–23)</label>
        <input type="number" id="dh-${t.id}" value="${t.deadline_hour||20}" min="0" max="23" style="width:70px">
        ${t.frequency==='weekly' ? `
        <label style="margin-left:12px">Day</label>
        <select id="dwd-${t.id}">
          ${DAYS.map((d,i)=>`<option value="${i}" ${i===(t.deadline_weekday??4)?'selected':''}>${d}</option>`).join('')}
        </select>` : ''}
        <button class="btn btn-sm btn-primary" style="margin-left:8px" onclick="saveDeadline(${t.id},'${t.frequency}')">Save</button>
        <button class="btn btn-sm btn-secondary" onclick="document.getElementById('dedit-${t.id}').style.display='none'">✕</button>
      </div>
    </div>`;
}

async function refreshTaskList() {
  const res   = await fetch('/api/tasks');
  const tasks = await res.json();

  const mandatory = tasks.filter(t => t.type === 'mandatory');
  const optional  = tasks.filter(t => t.type === 'optional');

  document.getElementById('tasks-list-mandatory').innerHTML =
    mandatory.length ? mandatory.map(renderTaskRow).join('') : '<div class="st-empty">No mandatory tasks</div>';
  document.getElementById('tasks-list-optional').innerHTML =
    optional.length  ? optional.map(renderTaskRow).join('')  : '<div class="st-empty">No optional tasks</div>';
}

function editDeadline(id) {
  const el = document.getElementById(`dedit-${id}`);
  el.style.display = el.style.display === 'none' ? 'block' : 'none';
}

async function saveDeadline(id, frequency) {
  const hour    = parseInt(document.getElementById(`dh-${id}`).value);
  const wdEl    = document.getElementById(`dwd-${id}`);
  const weekday = wdEl ? parseInt(wdEl.value) : 4;
  const body = { deadline_hour: hour, deadline_weekday: weekday };
  const res = await fetch(`/api/tasks/${id}`, {
    method: 'PUT', headers: authHeaders(), body: JSON.stringify(body),
  });
  if (res.ok) {
    toast('Deadline saved!', 'success');
    document.getElementById(`dedit-${id}`).style.display = 'none';
    refreshTaskList();
    loadDashboard();
  }
}

async function toggleTask(id, active) {
  await fetch(`/api/tasks/${id}`, {
    method:'PUT', headers: authHeaders(), body: JSON.stringify({ is_active: active }),
  });
  refreshTaskList();
  loadDashboard();
}

async function deleteTask(id) {
  if (!confirm('Delete this task permanently?')) return;
  const res = await fetch(`/api/tasks/${id}`, { method: 'DELETE', headers: authHeaders() });
  if (res.ok) {
    toast('Task deleted', 'success');
    refreshTaskList();
    loadDashboard();
  } else {
    const d = await res.json();
    toast(d.detail || 'Error', 'error');
  }
}

async function addTask() {
  const name      = document.getElementById('new-name').value.trim();
  const type      = document.getElementById('new-type').value;
  const frequency = document.getElementById('new-freq').value;
  const reward    = parseFloat(document.getElementById('new-reward').value);
  const dlHour    = parseInt(document.getElementById('new-dl-hour').value);
  const dlDay     = parseInt(document.getElementById('new-dl-day').value);
  if (!name) { toast('Enter a task name','error'); return; }
  const res = await fetch('/api/tasks', {
    method:'POST', headers: authHeaders(),
    body: JSON.stringify({ name, type, frequency, reward_dkk: reward,
                           deadline_hour: dlHour, deadline_weekday: dlDay }),
  });
  if (res.ok) {
    toast('Task added!','success');
    document.getElementById('new-name').value = '';
    refreshTaskList();
    loadDashboard();
  }
}


// ---------------------------------------------------------------------------
// Toast
// ---------------------------------------------------------------------------

function toast(msg, type='') {
  document.querySelectorAll('.toast').forEach(el=>el.remove());
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(()=>{ el.style.opacity='0'; setTimeout(()=>el.remove(),300); }, 2800);
}


// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

document.getElementById('auth-modal').addEventListener('click', e => {
  if (e.target === document.getElementById('auth-modal')) closeAuth();
});
document.getElementById('settings-modal').addEventListener('click', e => {
  if (e.target === document.getElementById('settings-modal')) closeSettings();
});

applyRole();
loadDashboard();
setInterval(loadDashboard, 60_000);
