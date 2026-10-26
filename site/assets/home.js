// "My timesheets": the signed-in person's months, saved to the office or kept on this
// computer. Open one to change it, delete one the office hasn't closed, or start a new one,
// with the jobs and other-time rows from an earlier month if they like.
// With ?as=<username>, an admin sees that engineer's page instead, read-only (see
// openViewAs in app.js).

import {
  $, el, toast, loadSession, clearSession, openSession, listSaved, deleteSaved, drafts, sheetTotals, monthInfo,
  shiftMonth, thisMonth, fmtHours, fmtDateTime, viewAsParam, openViewAs,
} from './app.js';

const session = loadSession();
if (!session?.uk) location.replace('index.html');

const viewing = viewAsParam();
const state = { bundle: null, profile: null, canSave: false, sheets: new Map(), loadError: null, viewAs: null };
const open = (month, copyFrom, withClosed) => `timesheet.html?month=${month}${copyFrom ? `&copyFrom=${copyFrom}` : ''}${
  copyFrom && withClosed ? '&closed=1' : ''}${state.viewAs ? `&as=${encodeURIComponent(state.viewAs.person.username)}` : ''}`;
const isClosed = (month) => (state.bundle?.closedMonths || []).includes(month);

function hasRows(data) {
  return Boolean(data?.jobs?.length || data?.other?.length);
}

// A copy on this computer that was saved, has no changes since, and is no longer in the
// office's list was removed by the office (or the store was started again): forget it.
// Recent saves are kept in case GitHub's search hasn't caught up yet.
const STALE_MS = 15 * 60 * 1000;
const removedByOffice = (record, savedMonths, month) => savedMonths && record?.meta?.savedAt && !record.meta.dirty
  && !savedMonths.has(month) && Date.now() - Date.parse(record.meta.savedAt) > STALE_MS;

// One entry per month: what's on this computer, merged with what's saved. `complete` means
// `saved` is the office's full list (it was fetched without errors).
function collect(saved, { complete = false } = {}) {
  const sheets = new Map();
  const savedMonths = complete ? new Set(saved.map((item) => item.month)) : null;
  for (const month of drafts.months(session.username)) {
    const record = drafts.get(session.username, month);
    if (removedByOffice(record, savedMonths, month)) {
      drafts.remove(session.username, month);
      continue;
    }
    if (!record || (!hasRows(record.data) && !record.meta?.savedAt)) continue;
    sheets.set(month, { month, local: record });
  }
  for (const item of saved) sheets.set(item.month, { ...(sheets.get(item.month) || { month: item.month }), saved: item });
  for (const sheet of sheets.values()) {
    const { local, saved: remote } = sheet;
    const localNewer = local?.meta?.dirty && (!remote || (local.meta.localUpdatedAt || '') > (remote.savedAt || ''));
    sheet.data = localNewer || !remote ? local.data : remote.data;
    sheet.totals = sheetTotals(sheet.data);
    if (localNewer && state.canSave) sheet.status = { tone: 'warn', text: 'Unsaved changes on this computer' };
    else if (remote || local?.meta?.savedAt) sheet.status = { tone: 'ok', text: `Saved ${fmtDateTime(remote?.savedAt || local?.meta?.savedAt)}` };
    else sheet.status = { tone: 'muted', text: state.canSave ? 'Not saved yet' : 'On this computer' };
    if (isClosed(sheet.month)) sheet.status = { tone: 'muted', text: 'Closed by the office' };
  }
  return sheets;
}

// Viewing as someone: only the months they've saved (the office can't see their computer).
function collectViewed() {
  const sheets = new Map();
  for (const [month, item] of state.viewAs.months) {
    sheets.set(month, {
      month, data: item.data, totals: sheetTotals(item.data),
      status: isClosed(month) ? { tone: 'muted', text: 'Closed by the office' } : { tone: 'ok', text: `Saved ${fmtDateTime(item.savedAt)}` },
    });
  }
  return sheets;
}

function showViewing() {
  const { person } = state.viewAs;
  const first = person.name.split(' ')[0];
  document.title = `${person.name}’s timesheets (view only) · Engineer Timewriting`;
  document.body.classList.add('viewing-as');
  $('#all-sheets').textContent = `${first}’s timesheets`;
  $('#sheets-hint').textContent = `What ${first} sees on My timesheets. Open a month to see their hours. Nothing here can be changed.`;
  $('#notices').replaceChildren(el('div', { class: 'notice notice-view' },
    el('div', { class: 'notice-text' }, el('strong', {}, `Viewing ${person.name}’s timesheets, read-only. `),
      `This is what they see, from the months they’ve saved${person.active ? '' : ' (they’ve left, so they can’t sign in)'}. `
      + 'Hours they haven’t saved yet are only on their own computer.'),
    el('a', { class: 'btn btn-sm', href: 'admin.html' }, 'Back to Admin')));
}

// ------------------------------------------------------------- the list

function render() {
  const months = [...state.sheets.keys()].sort().reverse();
  const body = $('#sheets');
  if (!months.length) {
    body.replaceChildren(el('p', { class: 'muted pad' }, state.viewAs ? 'They haven’t saved any timesheets yet.'
      : state.loadError ? 'Nothing on this computer yet.' : 'No timesheets yet. Press New timesheet to start one.'));
    return;
  }
  const rows = months.map((month) => {
    const item = state.sheets.get(month);
    const label = monthInfo(month).label;
    const actions = state.viewAs
      ? [el('a', { class: 'btn btn-sm', href: open(month) }, 'View')]
      : [el('a', { class: 'btn btn-sm', href: open(month) }, 'Open'),
        !isClosed(month) && el('button', { class: 'btn btn-sm btn-danger-ghost', type: 'button', 'aria-label': `Delete ${label}`,
          onclick: (e) => confirmDelete(e.currentTarget, month) }, 'Delete')];
    return el('tr', { dataset: { month } },
      el('th', { scope: 'row' }, el('a', { href: open(month) }, label),
        el('span', { class: `status-inline chip chip-${item.status.tone}` }, item.status.text)),
      el('td', {}, el('span', { class: `chip chip-${item.status.tone}` }, item.status.text)),
      el('td', { class: 'num' }, fmtHours(item.totals.jobs)),
      el('td', { class: 'num' }, fmtHours(item.totals.other)),
      el('td', { class: 'row-actions' }, ...actions.filter(Boolean)));
  });
  body.replaceChildren(el('table', { class: 'sheets' },
    el('thead', {}, el('tr', {},
      el('th', { scope: 'col' }, 'Month'), el('th', { scope: 'col' }, 'Status'),
      el('th', { scope: 'col', class: 'num' }, 'Job hours'), el('th', { scope: 'col', class: 'num' }, 'Other'),
      el('th', { scope: 'col' }, el('span', { class: 'sr-only' }, 'Actions')))),
    el('tbody', {}, rows)));
}

// ------------------------------------------------------------ deleting

// Asked in the row itself, so it's clear which month goes.
function confirmDelete(button, month) {
  document.querySelector('tr.confirm-row')?.remove();
  const row = button.closest('tr');
  const label = monthInfo(month).label;
  const sheet = state.sheets.get(month);
  // Saved to the office, even if GitHub's search hasn't listed it yet.
  const saved = Boolean(sheet.saved || sheet.local?.meta?.savedAt);
  const go = el('button', { class: 'btn btn-sm btn-danger', type: 'button' }, `Delete ${label}`);
  const cancel = el('button', { class: 'btn btn-ghost btn-sm', type: 'button', onclick: () => panel.remove() }, 'Cancel');
  const panel = el('tr', { class: 'confirm-row' }, el('td', { colspan: 5 },
    el('div', { class: 'confirm-panel', role: 'alertdialog', 'aria-label': `Delete ${label}?` },
      el('span', {}, el('strong', {}, `Delete ${label}? `), saved
        ? 'Its hours are taken out of the office’s journal and workbooks too. This can’t be undone.'
        : 'It’s only on this computer, so it’s simply removed. This can’t be undone.'),
      el('span', { class: 'confirm-actions' }, go, cancel))));
  go.addEventListener('click', async () => {
    go.disabled = true;
    cancel.disabled = true;
    go.textContent = 'Deleting…';
    try {
      const wasSaved = state.canSave
        ? await deleteSaved(state.bundle, state.profile, session.username, month, sheet.saved?.issue || sheet.local?.meta?.issue)
        : false;
      drafts.remove(session.username, month);
      state.sheets.delete(month);
      render();
      toast(wasSaved ? `Deleted ${label}. The journal updates in about a minute.` : `Deleted ${label}.`, { tone: 'ok' });
    } catch (error) {
      go.disabled = false;
      cancel.disabled = false;
      go.textContent = `Delete ${label}`;
      toast(error.message || 'Deleting didn’t work. Try again in a minute.', { tone: 'error', duration: 9000 });
    }
  });
  row.after(panel);
  cancel.focus();
}

// -------------------------------------------------------- a new timesheet

// Months that can be started: this month and the next two, except ones that already have
// a timesheet or are closed.
function startableMonths() {
  const current = thisMonth();
  const out = [];
  for (let m = shiftMonth(current, 2); m >= current; m = shiftMonth(m, -1)) {
    if (!state.sheets.has(m) && !isClosed(m)) out.push(m);
  }
  return out; // newest first
}

function showNewPanel() {
  const panel = $('#new-panel');
  if (!panel.hidden) {
    panel.hidden = true;
    return;
  }
  const current = thisMonth();
  const months = startableMonths();
  const sources = [...state.sheets.keys()].filter((m) => hasRows(state.sheets.get(m).data)).sort().reverse();
  if (!months.length) {
    panel.replaceChildren(el('p', { class: 'muted' }, 'This month and the next two already have timesheets. Open one of them below.'));
    panel.hidden = false;
    return;
  }
  // This month if it hasn't been started, otherwise the next free month (or the latest earlier one).
  const pick = months.includes(current) ? current : (months.filter((m) => m > current).pop() || months[0]);
  const monthSelect = el('select', { id: 'new-month', class: 'reuse-select' },
    months.map((m) => el('option', { value: m, selected: m === pick }, monthInfo(m).label)));
  const reuse = el('input', { type: 'radio', name: 'start-with', value: 'reuse', checked: sources.length > 0, disabled: !sources.length });
  const empty = el('input', { type: 'radio', name: 'start-with', value: 'empty', checked: !sources.length });
  const sourceSelect = el('select', { id: 'new-source', class: 'reuse-select', disabled: !sources.length, 'aria-label': 'Month to reuse' },
    sources.map((m) => el('option', { value: m }, monthInfo(m).label)));
  // Closed jobs (no longer open in Business Central) are left out unless ticked.
  const openJobs = new Set((state.bundle?.jobs || []).map((job) => job.no));
  const withClosed = el('input', { id: 'new-closed', type: 'checkbox' });
  const closedLabel = el('span', {});
  const closedRow = el('label', { class: 'check new-sub' }, withClosed, ' Also copy closed jobs ', closedLabel);
  const updateClosed = () => {
    const count = (state.sheets.get(sourceSelect.value)?.data?.jobs || []).filter((job) => !openJobs.has(job.job)).length;
    closedLabel.textContent = sources.length ? `(${count || 'none'} in ${monthInfo(sourceSelect.value).label})` : '';
    closedLabel.className = 'muted';
    withClosed.disabled = !reuse.checked || !count;
    if (withClosed.disabled) withClosed.checked = false;
  };
  let sourceTouched = false;
  const defaultSource = () => {
    if (!sourceTouched && sources.length) sourceSelect.value = sources.find((m) => m < monthSelect.value) || sources[0]; // the latest month before it
    updateClosed();
  };
  defaultSource();
  monthSelect.addEventListener('change', defaultSource);
  sourceSelect.addEventListener('change', () => {
    sourceTouched = true;
    reuse.checked = true;
    updateClosed();
  });
  reuse.addEventListener('change', updateClosed);
  empty.addEventListener('change', updateClosed);
  const startButton = el('button', { class: 'btn btn-primary', type: 'button', onclick: () => {
    const source = reuse.checked && sourceSelect.value ? sourceSelect.value : null;
    location.href = open(monthSelect.value, source, withClosed.checked);
  } }, 'Start timesheet');
  panel.replaceChildren(
    el('div', { class: 'new-grid' },
      el('label', { class: 'new-field' }, el('span', {}, 'Month'), monthSelect),
      el('fieldset', { class: 'new-choice' },
        el('legend', {}, 'Start with'),
        el('label', { class: 'check' }, reuse, ' Copy job template from ', sourceSelect),
        sources.length ? closedRow : el('p', { class: 'muted small' }, 'Nothing to reuse yet: your first timesheet starts empty.'),
        el('label', { class: 'check' }, empty, ' An empty timesheet'))),
    el('div', { class: 'new-actions' }, startButton,
      el('button', { class: 'btn btn-ghost', type: 'button', onclick: () => { panel.hidden = true; } }, 'Cancel')));
  panel.hidden = false;
  monthSelect.focus();
}

// ----------------------------------------------------------------- start

function notice(tone, text) {
  $('#notices').replaceChildren(el('div', { class: `notice notice-${tone}` }, el('div', { class: 'notice-text' }, text)));
}

async function start() {
  $('#user-name').textContent = session.name;
  try {
    const opened = await openSession(session);
    if (viewing) {
      if (!opened.profile.admin) {
        location.replace('home.html');
        return;
      }
      state.bundle = opened.bundle;
      $('#user-name').textContent = opened.profile.name;
      $('#admin-link').hidden = false;
      state.viewAs = await openViewAs(opened.bundle, opened.profile, viewing);
      showViewing();
      state.sheets = collectViewed();
      render();
      return;
    }
    if (!opened.profile.timesheets) {
      location.replace('admin.html');
      return;
    }
    state.bundle = opened.bundle;
    state.profile = opened.profile;
    session.name = opened.profile.name;
    $('#user-name').textContent = opened.profile.name;
    $('#admin-link').hidden = !opened.profile.admin;
  } catch (error) {
    if (error.code === 'signin') {
      clearSession();
      location.replace(`index.html?why=${error.reason || 'again'}`);
      return;
    }
    notice('error', error.message);
    return;
  }
  state.canSave = Boolean(state.bundle.saveToken && state.bundle.dataRepo);
  const updated = state.bundle.jobsUpdated
    ? new Date(`${state.bundle.jobsUpdated}T12:00:00`).toLocaleDateString('en-NZ', { day: 'numeric', month: 'short', year: 'numeric' })
    : '';
  $('#meta').textContent = `${state.bundle.jobs.length} open jobs from “${state.bundle.jobsSource}”${updated ? `, updated ${updated}` : ''}.`;
  $('#new-sheet').hidden = false;
  $('#new-sheet').addEventListener('click', showNewPanel);
  state.sheets = collect([]);
  render();
  if (!state.canSave) {
    notice('warn', 'Saving to the office isn’t switched on yet, so only the timesheets on this computer are shown.');
    return;
  }
  try {
    state.sheets = collect(await listSaved(state.bundle, state.profile, session.username), { complete: true });
  } catch (error) {
    state.loadError = error;
    notice('error', `${error.message} Showing the timesheets on this computer.`);
  }
  render();
}

if (session?.uk) start();
