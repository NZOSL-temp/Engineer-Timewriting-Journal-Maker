// The monthly timesheet: one row per job line (CAPEX/REVEX) and per other-time
// row, one column per day. Changes are kept on this computer straight away;
// Save sends the month to the office (a GitHub issue in the private data repo).
// With &as=<username>, an admin sees that engineer's saved month instead, read-only:
// nothing is kept on this computer and nothing can be saved (see openViewAs in app.js).

import {
  $, el, toast, TASKS, cleanText, fmtHours, parseHours, monthInfo, shiftMonth, thisMonth, isMonth,
  fmtDateTime, drafts, loadSession, openSession, loadSaved, listSaved, saveToGitHub, clearSession,
  journalLines, journalCells, journalTable, viewAsParam, openViewAs,
} from './app.js';
import { fireworks } from './fireworks.js';

const session = loadSession();
if (!session?.uk) location.replace(`index.html?next=${encodeURIComponent(`timesheet.html${location.search}`)}`);

const DOW = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const viewing = viewAsParam();
const state = {
  viewAs: null, // { person, months } when an admin is looking at someone's timesheets
  bundle: null,
  profile: null,
  jobs: new Map(),
  canSave: false,
  month: null,
  info: null,
  draft: { jobs: [], other: [] },
  meta: {},
  version: 0,
  invalid: new Map(),
  notices: new Map(),
  loadId: 0,
  saving: false,
  loading: false,
  jobFilter: '', // what's typed in the Jobs search box
};
let refs = {};
let uidCount = 0;
const uid = () => `r${(uidCount++).toString(36)}`;

// ------------------------------------------------------------ data shapes

function numbers(map) {
  const out = {};
  for (const [day, value] of Object.entries(map || {})) {
    const hours = Number(value);
    if (hours > 0) out[day] = Math.round(hours * 100) / 100;
  }
  return out;
}

function fromSaved(data) {
  const known = new Set(state.bundle.otherTime || []);
  return {
    jobs: (data.jobs || []).map((job) => ({
      uid: uid(),
      job: job.job,
      location: job.location ?? '',
      owner: job.owner ?? '',
      description: job.description ?? '',
      hours: { CAPEX: numbers(job.hours?.CAPEX), REVEX: numbers(job.hours?.REVEX) },
    })),
    other: (data.other || []).map((row) => ({
      uid: uid(), label: row.label || '', custom: !known.has(row.label), hours: numbers(row.hours),
    })),
  };
}

function toSaved(savedAt) {
  return {
    v: 1,
    employee: state.viewAs?.person.name ?? session.name,
    month: state.month,
    savedAt,
    jobs: state.draft.jobs.map((row) => {
      const hours = {};
      for (const task of TASKS) if (Object.keys(row.hours[task] || {}).length) hours[task] = numbers(row.hours[task]);
      return {
        job: row.job,
        location: cleanText(row.location),
        owner: cleanText(row.owner),
        description: cleanText(row.description),
        hours,
      };
    }),
    other: state.draft.other.map((row) => ({ label: cleanText(row.label), hours: numbers(row.hours) })),
  };
}

function tasksFor(row) {
  const listed = state.jobs.get(row.job)?.tasks || [];
  return TASKS.filter((task) => listed.includes(task) || Object.keys(row.hours[task] || {}).length);
}

// ----------------------------------------------------------- local copies

let localTimer;
function persistLocal() {
  clearTimeout(localTimer);
  if (!state.month || state.viewAs) return;
  drafts.set(session.username, state.month, { v: 1, data: toSaved(state.meta.savedAt || null), meta: state.meta });
}

function changed() {
  if (state.viewAs) return;
  state.version += 1;
  state.meta.dirty = true;
  state.meta.localUpdatedAt = new Date().toISOString();
  clearTimeout(localTimer);
  localTimer = setTimeout(persistLocal, 300);
  updateStatus();
}

// ------------------------------------------------------------------ notices

function setNotice(id, notice) {
  if (notice) state.notices.set(id, notice);
  else state.notices.delete(id);
  renderNotices();
}

function renderNotices() {
  const box = $('#notices');
  box.replaceChildren(...[...state.notices.values()].map((n) => el('div', { class: `notice notice-${n.tone}` },
    el('div', { class: 'notice-text' }, n.title ? el('strong', {}, n.title, ' ') : null, n.text),
    n.list ? el('ul', { class: 'notice-list' }, n.list.map((item) => el('li', {}, item))) : null,
    n.action ? el('button', { class: 'btn btn-sm', type: 'button', onclick: n.action.run }, n.action.label) : null)));
}

function checkJobs() {
  const problems = [];
  for (const row of state.draft.jobs) {
    const info = state.jobs.get(row.job);
    for (const task of TASKS) {
      const hours = Object.values(row.hours[task] || {}).reduce((a, b) => a + b, 0);
      if (!hours) continue;
      if (!info) problems.push(`${row.job} ${task} (${fmtHours(hours)} h): this job isn’t open in the latest job list.`);
      else if (!info.tasks.includes(task)) problems.push(`${row.job} ${task} (${fmtHours(hours)} h): the job is only open for ${info.tasks.join(' and ')}.`);
    }
  }
  setNotice('jobs', problems.length ? {
    tone: 'warn',
    title: 'Check these lines.',
    text: 'Jobs that are closed, or still at Quote or Planning, can’t take time. Ask the office to open the job, or move the hours.',
    list: problems,
  } : null);
}

// ------------------------------------------------------------------- status

function updateStatus() {
  const status = $('#save-status');
  const button = $('#save-btn');
  let text;
  let tone = 'muted';
  if (state.viewAs) {
    const first = state.viewAs.person.name.split(' ')[0];
    status.textContent = state.meta.savedAt ? `Viewing ${first}’s save of ${fmtDateTime(state.meta.savedAt)} (read-only)`
      : `${first} hasn’t saved ${state.info.label} (read-only)`;
    status.dataset.tone = 'muted';
    button.hidden = true;
    for (const id of ['#prev-month', '#next-month']) $(id).disabled = false;
    return;
  }
  if (isClosed()) text = 'Closed by the office';
  else if (state.saving) text = 'Saving…';
  else if (state.loading) text = 'Loading…';
  else if (!state.canSave) text = state.meta.dirty ? 'Changes kept on this computer' : 'Kept on this computer only';
  else if (state.meta.dirty) {
    text = 'Unsaved changes';
    tone = 'warn';
  } else if (state.meta.savedAt) {
    text = `Saved ${fmtDateTime(state.meta.savedAt)}`;
    tone = 'ok';
  } else text = 'Not saved yet';
  status.textContent = text;
  status.dataset.tone = tone;
  button.disabled = state.saving || state.loading || isClosed();
  button.textContent = state.saving ? 'Saving…' : 'Save';
  button.classList.toggle('attention', Boolean(state.meta.dirty && state.canSave));
  for (const id of ['#prev-month', '#next-month']) $(id).disabled = state.saving;
}

// ----------------------------------------------------------------- the grid

function dayClasses(day) {
  const { y, m } = state.info;
  const date = new Date(y, m - 1, day);
  const now = new Date();
  const classes = ['c-day'];
  if (date.getDay() === 0 || date.getDay() === 6) classes.push('weekend');
  if (date.getDay() === 1 && day > 1) classes.push('week-start');
  if (now.getFullYear() === y && now.getMonth() === m - 1 && now.getDate() === day) classes.push('today');
  return classes.join(' ');
}

function hoursCells(key, label, hours, rowIndex) {
  const cells = [];
  for (let day = 1; day <= state.info.days; day += 1) {
    const value = hours[day];
    const input = el('input', {
      class: 'cell',
      type: 'text',
      inputmode: 'decimal',
      autocomplete: 'off',
      spellcheck: false,
      value: value ? fmtHours(value) : '',
      placeholder: ' ',
      'aria-label': `${label}, ${DOW[new Date(state.info.y, state.info.m - 1, day).getDay()]} ${day}`,
      dataset: { key, day, r: rowIndex },
    });
    const bad = state.invalid.get(`${key}|${day}`);
    if (bad !== undefined) {
      input.value = bad;
      input.classList.add('invalid');
    }
    refs.cells.set(`${rowIndex}:${day}`, input);
    cells.push(el('td', { class: dayClasses(day) }, input));
  }
  return cells;
}

function lineRow(key, hours, label, rowIndex, cells) {
  refs.hours.set(key, hours);
  const total = el('td', { class: 'c-total sticky-3' });
  refs.lineTotals.set(key, total);
  const tr = el('tr', { class: 'line', dataset: { key } }, ...cells.lead, cells.type, total, ...hoursCells(key, label, hours, rowIndex));
  refs.rows.set(key, tr);
  return tr;
}

function removeButton(kind, row, label) {
  return el('button', {
    class: 'icon-btn remove-row', type: 'button', title: `Remove ${label}`, 'aria-label': `Remove ${label}`,
    dataset: { kind, uid: row.uid },
  }, '×');
}

function render() {
  const { days } = state.info;
  refs = { cells: new Map(), hours: new Map(), lineTotals: new Map(), rows: new Map(), dayHeads: [], dayTotals: [] };
  const cols = [el('col', { class: 'col-label' }), el('col', { class: 'col-type' }), el('col', { class: 'col-total' })];
  const head = [
    el('th', { class: 'c-label sticky-1', scope: 'col' }, 'Job / other time'),
    el('th', { class: 'c-type sticky-2', scope: 'col' }, 'Capex/Revex'),
    el('th', { class: 'c-total sticky-3', scope: 'col' }, 'Total'),
  ];
  const totals = [
    el('th', { class: 'c-label sticky-1 totals-label', scope: 'row' }, 'Daily total'),
    el('th', { class: 'c-type sticky-2' }),
    (refs.grand = el('th', { class: 'c-total sticky-3 grand' })),
  ];
  for (let day = 1; day <= days; day += 1) {
    cols.push(el('col', { class: 'col-day' }));
    const date = new Date(state.info.y, state.info.m - 1, day);
    const th = el('th', { class: dayClasses(day), scope: 'col', dataset: { day } },
      el('span', { class: 'dow' }, DOW[date.getDay()]), el('span', { class: 'dom' }, day));
    refs.dayHeads[day] = th;
    head.push(th);
    refs.dayTotals[day] = el('th', { class: `${dayClasses(day)} daytotal`, dataset: { day } });
    totals.push(refs.dayTotals[day]);
  }

  // Other time first, then jobs, as in the engineers' spreadsheet.
  let rowIndex = 0;
  const otherBody = el('tbody', { class: 'section-other' },
    sectionRow('Other time', 'Leave, general time and so on. Not in the journal.'));
  for (const row of state.draft.other) {
    const labelCell = el('td', { class: 'c-label sticky-1 other-cell' },
      el('div', { class: 'other-head' },
        row.custom
          ? el('input', {
            class: 'detail other-label', value: row.label, maxLength: 100, placeholder: 'Describe the time, e.g. Special project',
            'aria-label': 'Description of other time', dataset: { uid: row.uid },
          })
          : el('span', { class: 'other-name' }, row.label),
        removeButton('other', row, row.label || 'this row')));
    otherBody.append(lineRow(`o:${row.uid}:-`, row.hours, row.label || 'Other time', rowIndex, {
      lead: [labelCell],
      type: el('td', { class: 'c-type sticky-2 muted' }, '—'),
    }));
    rowIndex += 1;
  }
  otherBody.append(el('tr', { class: 'adder-row' },
    el('td', { class: 'sticky-1 adder-cell', colspan: 3 }, otherAdder()), el('td', { class: 'adder-fill', colspan: days })));

  const jobBody = el('tbody', { class: 'section-jobs' },
    sectionRow('Jobs', null, jobFilterInput()));
  for (const row of state.draft.jobs) {
    const info = state.jobs.get(row.job);
    const tasks = tasksFor(row);
    const label = el('td', { class: 'c-label sticky-1 job-cell', rowspan: Math.max(tasks.length, 1) },
      el('div', { class: 'job-box' },
        el('div', { class: 'job-head' },
          el('span', { class: 'job-no' }, row.job),
          info ? null : el('span', { class: 'badge badge-warn', title: 'Not in the latest list of open jobs' }, 'Not open'),
          removeButton('job', row, row.job)),
        el('div', { class: 'job-details' },
          detailInput(row, 'location', 'Location', 'loc'),
          detailInput(row, 'owner', 'Owner', 'owner'),
          detailInput(row, 'description', 'Description', 'desc'))));
    if (!tasks.length) {
      jobBody.append(el('tr', { class: 'line closed', dataset: { job: row.uid } }, label,
        el('td', { class: 'c-type sticky-2' }, '—'), el('td', { class: 'c-total sticky-3' }),
        el('td', { class: 'closed-note', colspan: days }, 'This job isn’t open any more. Remove it, or ask for it to be reopened.')));
      continue;
    }
    tasks.forEach((task, i) => {
      row.hours[task] ||= {};
      const listed = info?.tasks.includes(task);
      const pill = el('span', { class: `pill pill-${task.toLowerCase()}${listed ? '' : ' pill-problem'}`, title: listed ? '' : `${row.job} isn’t open for ${task}` }, task);
      const tr = lineRow(`j:${row.uid}:${task}`, row.hours[task], `${row.job} ${task}`, rowIndex, {
        lead: i === 0 ? [label] : [],
        type: el('td', { class: 'c-type sticky-2' }, pill),
      });
      tr.dataset.job = row.uid;
      jobBody.append(tr);
      rowIndex += 1;
    });
  }
  if (!state.draft.jobs.length) jobBody.append(emptyRow());
  jobBody.append(el('tr', { class: 'adder-row' },
    el('td', { class: 'sticky-1 adder-cell', colspan: 3 }, jobAdder()), el('td', { class: 'adder-fill', colspan: days })));

  const table = el('table', { class: 'grid' },
    el('colgroup', {}, cols),
    el('thead', {}, el('tr', { class: 'days' }, head), el('tr', { class: 'totals' }, totals)),
    otherBody,
    jobBody);
  refs.rowCount = rowIndex;
  const wrap = $('#grid-wrap');
  const scroll = { left: wrap.scrollLeft, top: wrap.scrollTop };
  wrap.replaceChildren(table);
  wrap.scrollLeft = scroll.left;
  wrap.scrollTop = scroll.top;
  const locked = isReadOnly();
  wrap.classList.toggle('readonly', locked);
  if (locked) wrap.querySelectorAll('input:not(.job-filter), select, button').forEach((node) => { node.disabled = true; });
  applyJobFilter();
  const closed = isClosed();
  setNotice('closed', closed ? {
    tone: 'info',
    title: `${state.info.label} is closed.`,
    text: 'Its journal has gone to Business Central, so it can’t be changed. You can still copy its rows into another month.',
  } : null);
  updateTotals();
  checkJobs();
}

function sectionRow(title, hint, control) {
  return el('tr', { class: 'section-row' },
    el('th', { class: 'sticky-1 section-title', colspan: 3, scope: 'rowgroup' },
      el('div', { class: 'section-head' }, el('span', {}, title, hint ? el('span', { class: 'section-hint' }, hint) : null), control)),
    el('td', { class: 'section-fill', colspan: state.info.days }));
}

// The search box in the Jobs header: shows only the jobs that match, to find one quickly.
function jobFilterInput() {
  return el('input', {
    class: 'job-filter', type: 'search', placeholder: 'Search your jobs', value: state.jobFilter || '',
    'aria-label': 'Search your jobs by number, location, owner or description', autocomplete: 'off', spellcheck: false,
    oninput: (event) => {
      state.jobFilter = event.currentTarget.value;
      applyJobFilter();
    },
  });
}

// Every word typed must appear in the job's number (JOB009683, 9683, J9683; "J9683-DU-80" as in
// the old spreadsheets also works), location, owner, description or CAPEX/REVEX.
function applyJobFilter() {
  const words = cleanText(state.jobFilter || '').toLowerCase().split(/[\s\-–—]+/).filter(Boolean);
  let shown = 0;
  for (const row of state.draft.jobs) {
    const digits = row.job.replace(/^JOB0*/, '');
    const text = `${row.job} job${digits} j${digits} ${row.location} ${row.owner} ${row.description} ${tasksFor(row).join(' ')}`.toLowerCase();
    const match = words.every((word) => text.includes(word));
    if (match) shown += 1;
    document.querySelectorAll(`.section-jobs tr[data-job="${row.uid}"]`).forEach((tr) => { tr.hidden = !match; });
  }
  const body = document.querySelector('.section-jobs');
  body?.querySelector('.filter-empty')?.remove();
  if (body && words.length && !shown && state.draft.jobs.length) {
    const adder = body.querySelector('.adder-row');
    adder?.before(el('tr', { class: 'filter-empty' },
      el('td', { class: 'sticky-1 empty-cell', colspan: 3 }, el('p', {}, `No jobs match “${state.jobFilter.trim()}”.`)),
      el('td', { class: 'adder-fill', colspan: state.info.days })));
  }
}

function emptyRow() {
  return el('tr', { class: 'empty-row' },
    el('td', { class: 'sticky-1 empty-cell', colspan: 3 },
      el('p', {}, isReadOnly() ? 'No jobs were saved for this month.' : 'No jobs yet. Type a job number below.')),
    el('td', { class: 'adder-fill', colspan: state.info.days }));
}

function detailInput(row, field, label, cls) {
  return el('input', {
    class: `detail ${cls}`, value: row[field] || '', placeholder: label, title: row[field] || label,
    'aria-label': `${label} for ${row.job}`, maxLength: field === 'description' ? 200 : 20,
    dataset: { uid: row.uid, field },
  });
}

function updateTotals() {
  const dayTotals = new Array(state.info.days + 1).fill(0);
  let jobs = 0;
  let other = 0;
  for (const [key, hours] of refs.hours) {
    let total = 0;
    for (const [day, value] of Object.entries(hours)) {
      total += value;
      dayTotals[day] += value;
    }
    refs.lineTotals.get(key).textContent = fmtHours(total);
    if (key.startsWith('j:')) jobs += total;
    else other += total;
  }
  for (let day = 1; day <= state.info.days; day += 1) {
    const cell = refs.dayTotals[day];
    cell.textContent = dayTotals[day] ? fmtHours(dayTotals[day]) : '';
    cell.classList.toggle('over', dayTotals[day] > 24);
  }
  refs.grand.textContent = fmtHours(jobs + other);
  $('#totals').replaceChildren(
    el('span', {}, 'Jobs ', el('strong', {}, fmtHours(jobs)), ' h'),
    el('span', {}, 'Other ', el('strong', {}, fmtHours(other)), ' h'),
    el('span', {}, 'Total ', el('strong', {}, fmtHours(jobs + other)), ' h'));
  refs.dayTotalValues = dayTotals;
}

// ---------------------------------------------------------- editing cells

function setCell(input, text) {
  const { key, day } = input.dataset;
  const hours = refs.hours.get(key);
  const parsed = parseHours(text);
  if (parsed.ok) {
    state.invalid.delete(`${key}|${day}`);
    input.classList.remove('invalid');
    input.title = '';
    if (parsed.value) hours[day] = parsed.value;
    else delete hours[day];
  } else {
    state.invalid.set(`${key}|${day}`, text);
    input.classList.add('invalid');
    input.title = parsed.message;
    delete hours[day];
  }
  return parsed.ok;
}

function onGridInput(event) {
  const target = event.target;
  if (target.classList.contains('cell')) {
    setCell(target, target.value);
    updateTotals();
    changed();
  } else if (target.dataset.field) {
    const row = state.draft.jobs.find((r) => r.uid === target.dataset.uid);
    if (row) row[target.dataset.field] = target.value;
    target.title = target.value;
    changed();
  } else if (target.classList.contains('other-label')) {
    const row = state.draft.other.find((r) => r.uid === target.dataset.uid);
    if (row) row.label = target.value;
    changed();
  }
}

function onGridBlur(event) {
  const target = event.target;
  if (target.classList.contains('cell') && !target.classList.contains('invalid')) {
    const value = refs.hours.get(target.dataset.key)?.[target.dataset.day];
    target.value = value ? fmtHours(value) : '';
  }
}

function moveTo(input, rowStep, dayStep) {
  let row = Number(input.dataset.r) + rowStep;
  const day = Number(input.dataset.day) + dayStep;
  let target = refs.cells.get(`${row}:${day}`);
  while (target && rowStep && target.closest('tr').hidden) { // skip jobs hidden by the search
    row += rowStep;
    target = refs.cells.get(`${row}:${day}`);
  }
  if (target) {
    target.focus();
    target.select();
  }
  return Boolean(target);
}

function onGridKeydown(event) {
  const input = event.target;
  if (!input.classList.contains('cell')) return;
  const atStart = input.selectionStart === 0 && input.selectionEnd === 0;
  const atEnd = input.selectionStart === input.value.length;
  const all = input.selectionStart === 0 && input.selectionEnd === input.value.length;
  const moves = {
    ArrowUp: [-1, 0, true],
    ArrowDown: [1, 0, true],
    ArrowLeft: [0, -1, atStart || all],
    ArrowRight: [0, 1, atEnd || all],
    Enter: [event.shiftKey ? -1 : 1, 0, true],
  };
  const move = moves[event.key];
  if (move && move[2] && !event.altKey && !event.metaKey && !event.ctrlKey) {
    event.preventDefault();
    moveTo(input, move[0], move[1]);
  }
}

// Paste a block copied from Excel: tabs between days, new lines between rows.
function onGridPaste(event) {
  const input = event.target;
  if (!input.classList.contains('cell')) return;
  const text = event.clipboardData?.getData('text') ?? '';
  if (!/[\t\n]/.test(text.trim())) return;
  event.preventDefault();
  const rows = text.replace(/\r/g, '').replace(/\n$/, '').split('\n').map((line) => line.split('\t'));
  const startRow = Number(input.dataset.r);
  const startDay = Number(input.dataset.day);
  // The rows on screen from this one down (the search may be hiding some).
  const targets = [];
  for (let r = startRow; targets.length < rows.length && r < (refs.rowCount || 0); r += 1) {
    const first = refs.cells.get(`${r}:1`);
    if (first && !first.closest('tr').hidden) targets.push(r);
  }
  let bad = 0;
  rows.forEach((values, i) => values.forEach((value, j) => {
    const cell = targets[i] === undefined ? null : refs.cells.get(`${targets[i]}:${startDay + j}`);
    if (!cell) return;
    cell.value = value.trim();
    if (!setCell(cell, value)) bad += 1;
    else if (refs.hours.get(cell.dataset.key)[cell.dataset.day]) cell.value = fmtHours(refs.hours.get(cell.dataset.key)[cell.dataset.day]);
  }));
  updateTotals();
  changed();
  if (bad) toast(`${bad} pasted value${bad === 1 ? '' : 's'} weren’t hours and are highlighted.`, { tone: 'warn' });
}

function onGridFocus(event) {
  const input = event.target;
  for (const node of document.querySelectorAll('.focus-col, .focus-row')) node.classList.remove('focus-col', 'focus-row');
  if (!input.classList.contains('cell')) return;
  refs.dayHeads[input.dataset.day]?.classList.add('focus-col');
  refs.dayTotals[input.dataset.day]?.classList.add('focus-col');
  refs.rows.get(input.dataset.key)?.classList.add('focus-row');
  input.select();
}

function onGridClick(event) {
  const button = event.target.closest('.remove-row');
  if (!button) return;
  const list = button.dataset.kind === 'job' ? state.draft.jobs : state.draft.other;
  const index = list.findIndex((row) => row.uid === button.dataset.uid);
  if (index < 0) return;
  const [row] = list.splice(index, 1);
  const prefix = `${button.dataset.kind === 'job' ? 'j' : 'o'}:${row.uid}:`;
  for (const key of [...state.invalid.keys()]) if (key.startsWith(prefix)) state.invalid.delete(key);
  changed();
  render();
  const name = row.job || row.label || 'Row';
  toast(`${name} removed.`, {
    action: {
      label: 'Undo',
      run: () => {
        list.splice(index, 0, row);
        changed();
        render();
      },
    },
    duration: 7000,
  });
}

// ------------------------------------------------------------- adding rows

function firstCellFor(keyPrefix) {
  const now = new Date();
  const day = now.getFullYear() === state.info.y && now.getMonth() === state.info.m - 1 ? now.getDate() : 1;
  for (const [key, tr] of refs.rows) {
    if (key.startsWith(keyPrefix)) return tr.querySelector(`.cell[data-day="${day}"]`);
  }
  return null;
}

function flash(keyPrefix) {
  for (const [key, tr] of refs.rows) if (key.startsWith(keyPrefix)) tr.classList.add('flash');
  setTimeout(() => document.querySelectorAll('tr.flash').forEach((tr) => tr.classList.remove('flash')), 1200);
}

function addJob(number) {
  if (state.jobFilter) { // otherwise the job could be hidden by the search
    state.jobFilter = '';
    render();
  }
  const existing = state.draft.jobs.find((row) => row.job === number);
  if (existing) {
    flash(`j:${existing.uid}:`);
    firstCellFor(`j:${existing.uid}:`)?.focus();
    return;
  }
  const info = state.jobs.get(number);
  const row = {
    uid: uid(), job: number, location: info.loc, owner: info.owner, description: info.desc, hours: { CAPEX: {}, REVEX: {} },
  };
  state.draft.jobs.push(row);
  changed();
  render();
  flash(`j:${row.uid}:`);
  const cell = firstCellFor(`j:${row.uid}:`);
  cell?.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  cell?.focus();
}

function matchJobs(query) {
  const text = query.trim().toUpperCase();
  const digits = text.replace(/\D/g, '');
  const jobs = state.bundle.jobs;
  if (!digits) return text && !'JOB'.startsWith(text.slice(0, 3)) ? [] : jobs;
  const scored = [];
  for (const job of jobs) {
    const number = job.no.slice(3);
    const trimmed = number.replace(/^0+/, '');
    if (!number.includes(digits) && !trimmed.includes(digits)) continue;
    const score = trimmed === digits.replace(/^0+/, '') ? 0 : trimmed.startsWith(digits) ? 1 : number.endsWith(digits) ? 2 : 3;
    scored.push([score, job]);
  }
  return scored.sort((a, b) => a[0] - b[0] || a[1].no.localeCompare(b[1].no)).map(([, job]) => job);
}

function jobAdder() {
  const list = $('#job-options');
  const input = el('input', {
    id: 'job-search', class: 'combo-input', type: 'text', role: 'combobox', autocomplete: 'off', spellcheck: false,
    'aria-expanded': 'false', 'aria-controls': 'job-options', 'aria-autocomplete': 'list',
    placeholder: 'Add a job: type its number, e.g. 9683', 'aria-label': 'Add a job by job number',
  });
  const hint = el('div', { class: 'combo-hint', role: 'status' });
  let matches = [];
  let active = -1;

  const place = () => {
    const box = input.getBoundingClientRect();
    list.style.left = `${box.left}px`;
    list.style.width = `${Math.max(box.width, 220)}px`;
    const below = window.innerHeight - box.bottom;
    if (below < 240 && box.top > below) {
      list.style.top = '';
      list.style.bottom = `${window.innerHeight - box.top + 4}px`;
    } else {
      list.style.bottom = '';
      list.style.top = `${box.bottom + 4}px`;
    }
  };
  const close = () => {
    list.hidden = true;
    input.setAttribute('aria-expanded', 'false');
    input.removeAttribute('aria-activedescendant');
  };
  const highlight = (index) => {
    active = index;
    [...list.children].forEach((li, i) => li.setAttribute('aria-selected', String(i === index)));
    const li = list.children[index];
    if (li?.id) {
      input.setAttribute('aria-activedescendant', li.id);
      li.scrollIntoView({ block: 'nearest' });
    }
  };
  const open = () => {
    matches = matchJobs(input.value);
    hint.textContent = '';
    const added = new Set(state.draft.jobs.map((row) => row.job));
    list.replaceChildren(...matches.map((job) => el('li', {
      id: `opt-${job.no}`, role: 'option', 'aria-selected': 'false', dataset: { no: job.no },
    }, el('span', {}, job.no), added.has(job.no) ? el('span', { class: 'opt-tag' }, 'added') : null)));
    if (!matches.length) {
      list.append(el('li', { class: 'opt-empty', role: 'presentation' }, 'No open job matches that number.'));
    }
    list.hidden = false;
    input.setAttribute('aria-expanded', 'true');
    place();
    highlight(matches.length && input.value.trim() ? 0 : -1);
  };
  const choose = (number) => {
    close();
    input.value = '';
    addJob(number);
  };

  input.addEventListener('focus', open);
  input.addEventListener('input', open);
  input.addEventListener('blur', () => setTimeout(close, 120));
  input.addEventListener('keydown', (event) => {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      if (list.hidden) open();
      const step = event.key === 'ArrowDown' ? 1 : -1;
      if (matches.length) highlight((active + step + matches.length) % matches.length);
    } else if (event.key === 'Enter') {
      event.preventDefault();
      if (active >= 0 && matches[active]) choose(matches[active].no);
      else if (matches.length === 1) choose(matches[0].no);
      else if (input.value.trim()) {
        hint.textContent = 'No open job has that number. Closed jobs, and jobs still at Quote or Planning, can’t take time. Ask the office to open it.';
      }
    } else if (event.key === 'Escape') {
      close();
    }
  });
  list.onmousedown = (event) => {
    const li = event.target.closest('li[data-no]');
    if (!li) return;
    event.preventDefault();
    choose(li.dataset.no);
  };
  return el('div', { class: 'combo' }, el('span', { class: 'combo-plus', 'aria-hidden': 'true' }, '+'), input, hint);
}

function otherAdder() {
  const used = new Set(state.draft.other.map((row) => row.label));
  const select = el('select', { class: 'other-select', 'aria-label': 'Add other time' },
    el('option', { value: '' }, 'Add other time…'),
    ...(state.bundle.otherTime || []).filter((label) => !used.has(label)).map((label) => el('option', { value: label }, label)),
    el('option', { value: '__custom__' }, 'Something else…'));
  select.addEventListener('change', () => {
    const value = select.value;
    if (!value) return;
    const row = { uid: uid(), label: value === '__custom__' ? '' : value, custom: value === '__custom__', hours: {} };
    state.draft.other.push(row);
    changed();
    render();
    flash(`o:${row.uid}:`);
    if (row.custom) refs.rows.get(`o:${row.uid}:-`)?.querySelector('.other-label')?.focus();
    else firstCellFor(`o:${row.uid}:`)?.focus();
  });
  return el('div', { class: 'other-adder' }, el('span', { class: 'combo-plus', 'aria-hidden': 'true' }, '+'), select);
}

// Reuse another month's sheet: its jobs and other-time rows, without the hours.
// Copy another month's jobs and other-time rows (not the hours). Jobs that aren't open any
// more are left out, unless `includeClosed` (New timesheet's "Also copy closed jobs").
async function copyRows(source, { includeClosed = false } = {}) {
  // The newer of the copy on this computer and the office's saved copy (it may have been
  // changed on another computer since).
  const local = drafts.get(session.username, source);
  let data = local?.data;
  if (state.canSave) {
    try {
      const saved = (await loadSaved(state.bundle, state.profile, session.username, source))?.data;
      const localNewer = local?.meta?.dirty && (local.meta.localUpdatedAt || '') > (saved?.savedAt || '');
      if (saved && !localNewer) data = saved;
    } catch {
      /* the copy on this computer, if there is one */
    }
  }
  if (!data?.jobs?.length && !data?.other?.length) {
    toast(`Nothing was found for ${monthInfo(source).label}.`);
    return;
  }
  const have = new Set(state.draft.jobs.map((row) => row.job));
  let jobs = 0;
  let skipped = 0;
  for (const old of data.jobs || []) {
    const info = state.jobs.get(old.job);
    if (have.has(old.job)) continue;
    if (!info && !includeClosed) {
      skipped += 1;
      continue;
    }
    state.draft.jobs.push({
      uid: uid(), job: old.job, location: info?.loc ?? old.location ?? '', owner: info?.owner ?? old.owner ?? '',
      description: info?.desc ?? old.description ?? '', hours: { CAPEX: {}, REVEX: {} },
    });
    have.add(old.job);
    jobs += 1;
  }
  const labels = new Set(state.draft.other.map((row) => row.label));
  let other = 0;
  for (const old of data.other || []) {
    if (!old.label || labels.has(old.label)) continue;
    state.draft.other.push({ uid: uid(), label: old.label, custom: !(state.bundle.otherTime || []).includes(old.label), hours: {} });
    other += 1;
  }
  changed();
  render();
  toast(`Copied ${jobs} job${jobs === 1 ? '' : 's'} and ${other} other row${other === 1 ? '' : 's'} from ${monthInfo(source).label}${skipped ? `; ${skipped} closed job${skipped === 1 ? '' : 's'} left out` : ''}.`, { duration: 6000 });
}

// ------------------------------------------------------------------ saving

function problems() {
  if (state.invalid.size) {
    const [key] = state.invalid.keys();
    const [lineKey, day] = key.split('|');
    return [{ text: 'Some cells aren’t hours. Fix the highlighted cells first.', focus: refs.rows.get(lineKey)?.querySelector(`.cell[data-day="${day}"]`) }];
  }
  const over = (refs.dayTotalValues || []).findIndex((v) => v > 24);
  if (over > 0) return [{ text: `Day ${over} adds up to more than 24 hours.`, focus: null }];
  const unnamed = state.draft.other.find((row) => row.custom && !cleanText(row.label));
  if (unnamed) return [{ text: 'Give each “Something else” row a description.', focus: refs.rows.get(`o:${unnamed.uid}:-`)?.querySelector('.other-label') }];
  return [];
}

async function save() {
  if (state.viewAs || state.saving || state.loading) return; // viewing someone else's: never saves
  persistLocal();
  const [problem] = problems();
  if (problem) {
    toast(problem.text, { tone: 'error' });
    problem.focus?.focus();
    return;
  }
  if (isClosed()) {
    toast('This month is closed, so it can’t be changed.', { tone: 'warn' });
    return;
  }
  if (!state.canSave) {
    toast('Kept on this computer. Saving to the office isn’t switched on yet.', { tone: 'warn' });
    return;
  }
  state.saving = true;
  updateStatus();
  const version = state.version;
  const data = toSaved(new Date().toISOString());
  try {
    const issue = await saveToGitHub(state.bundle, state.profile, session.username, data);
    state.meta.savedAt = data.savedAt;
    state.meta.issue = issue.number;
    if (state.version === version) state.meta.dirty = false;
    persistLocal();
    setNotice('newer-local', null);
    toast('Saved. The journal and workbooks update in about a minute.', { tone: 'ok' });
    fireworks();
  } catch (error) {
    toast(error.message || 'Saving didn’t work. Your hours are still on this computer.', { tone: 'error', duration: 9000 });
  } finally {
    state.saving = false;
    updateStatus();
  }
}

// The month's journal lines for this engineer, exactly as the journal will have them.
function showPreview() {
  const rate = state.viewAs ? state.viewAs.person.rate : state.profile.rate;
  const lines = journalLines(toSaved(state.meta.savedAt || new Date().toISOString()), rate, state.bundle.journal);
  const hours = lines.reduce((sum, line) => sum + (line.hours || 0), 0);
  $('#preview-title').textContent = `Journal preview · ${state.info.label}`;
  $('#preview-body').replaceChildren(
    el('p', { class: 'muted' }, 'Your lines in the timewriting journal for Business Central, as they’ll appear once you save. '
      + 'The yellow columns come from your timesheet; the recharge rate is set by the office.'),
    lines.length
      ? journalTable(lines.map((line) => ({ cells: journalCells(line), balancing: line.balancing })))
      : el('p', {}, 'No job hours yet, so there are no journal lines.'),
    lines.length ? el('p', { class: 'preview-total' }, `${lines.length - 1} line${lines.length === 2 ? '' : 's'}, ${fmtHours(hours)} hours.`) : null);
  $('#preview').showModal();
}

// ------------------------------------------------------------------ months

async function openMonth(month) {
  persistLocal();
  state.jobFilter = '';
  state.month = month;
  state.info = monthInfo(month);
  state.invalid.clear();
  history.replaceState(null, '', `?month=${month}${state.viewAs ? `&as=${encodeURIComponent(state.viewAs.person.username)}` : ''}`);
  $('#month-label').textContent = state.info.label;
  document.title = `${state.info.label} · Engineer Timewriting`;
  if (state.viewAs) {
    const saved = state.viewAs.months.get(month);
    state.draft = saved ? fromSaved(saved.data) : { jobs: [], other: [] };
    state.meta = saved ? { savedAt: saved.savedAt } : {};
    render();
    updateStatus();
    return;
  }
  const record = drafts.get(session.username, month);
  state.draft = record ? fromSaved(record.data) : { jobs: [], other: [] };
  state.meta = record?.meta ? { ...record.meta } : {};
  setNotice('newer-local', null);
  setNotice('load', null);
  render();
  updateStatus();
  if (!state.canSave) return;

  // Fetch the saved copy; the grid is locked meanwhile so nothing typed is lost.
  const loadId = ++state.loadId;
  state.loading = true;
  setBusy(true);
  updateStatus();
  try {
    const saved = await loadSaved(state.bundle, state.profile, session.username, month);
    if (loadId !== state.loadId) return;
    if (saved?.unverified) {
      setNotice('load', {
        tone: 'error',
        title: 'Your saved copy couldn’t be verified.',
        text: 'Someone may have changed it. The journal still uses your last verified save; this page shows the copy on this computer. Press Save to store it again.',
      });
    } else if (saved) {
      const savedAt = saved.data.savedAt || '';
      if (state.meta.dirty && (state.meta.localUpdatedAt || '') > savedAt) {
        setNotice('newer-local', {
          tone: 'info',
          text: `This computer has changes made after your last save (${fmtDateTime(savedAt)}). Press Save to keep them, or load the saved copy.`,
          action: { label: 'Load saved copy', run: () => useSaved(saved) },
        });
      } else {
        useSaved(saved, false);
      }
    }
  } catch (error) {
    if (loadId !== state.loadId) return;
    setNotice('load', { tone: 'error', text: `${error.message} Showing the copy on this computer.` });
  } finally {
    if (loadId === state.loadId) {
      state.loading = false;
      setBusy(false);
    }
  }
  if (loadId !== state.loadId) return;
  render();
  updateStatus();
}

function setBusy(on) {
  const wrap = $('#grid-wrap');
  wrap.classList.toggle('busy', on);
  wrap.inert = on;
}

function useSaved(saved, rerender = true) {
  state.draft = fromSaved(saved.data);
  state.meta = { dirty: false, savedAt: saved.data.savedAt, issue: saved.issue.number };
  state.invalid.clear();
  persistLocal();
  setNotice('newer-local', null);
  if (rerender) {
    render();
    updateStatus();
  }
}

function goMonth(delta) {
  if (state.saving) return;
  openMonth(shiftMonth(state.month, delta));
}

// -------------------------------------------------------------------- start

async function start() {
  $('#user-name').textContent = session.name;
  try {
    const opened = await openSession(session);
    if (viewing) {
      if (!opened.profile.admin) {
        location.replace('home.html');
        return;
      }
      state.viewAs = await openViewAs(opened.bundle, opened.profile, viewing);
    } else if (!opened.profile.timesheets) {
      location.replace('admin.html');
      return;
    }
    state.bundle = opened.bundle;
    state.profile = opened.profile;
    session.name = opened.profile.name;
    $('#user-name').textContent = opened.profile.name;
  } catch (error) {
    if (error.code === 'signin') {
      clearSession();
      location.replace(`index.html?why=${error.reason || 'again'}`);
      return;
    }
    $('#loading').textContent = error.message;
    return;
  }
  state.jobs = new Map(state.bundle.jobs.map((job) => [job.no, job]));
  state.canSave = Boolean(state.bundle.saveToken && state.bundle.dataRepo);
  if (!state.canSave) {
    setNotice('no-save', {
      tone: 'warn',
      title: 'Saving to the office isn’t switched on yet.',
      text: 'Your hours are kept in this browser on this computer until it is.',
    });
  }
  const updated = state.bundle.jobsUpdated
    ? new Date(`${state.bundle.jobsUpdated}T12:00:00`).toLocaleDateString('en-NZ', { day: 'numeric', month: 'short', year: 'numeric' })
    : '';
  $('#meta').textContent = `${state.bundle.jobs.length} open jobs from “${state.bundle.jobsSource}”${updated ? `, updated ${updated}` : ''}.`;

  const wrap = $('#grid-wrap');
  wrap.addEventListener('input', onGridInput);
  wrap.addEventListener('keydown', onGridKeydown);
  wrap.addEventListener('paste', onGridPaste);
  wrap.addEventListener('focusin', onGridFocus);
  wrap.addEventListener('focusout', onGridBlur);
  wrap.addEventListener('click', onGridClick);
  $('#prev-month').addEventListener('click', () => goMonth(-1));
  $('#next-month').addEventListener('click', () => goMonth(1));
  $('#save-btn').addEventListener('click', save);
  $('#preview-btn').addEventListener('click', showPreview);
  $('#preview-close').addEventListener('click', () => $('#preview').close());
  document.addEventListener('keydown', (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 's') {
      event.preventDefault();
      save();
    }
  });
  window.addEventListener('beforeunload', (event) => {
    persistLocal();
    if (state.canSave && state.meta.dirty) {
      event.preventDefault();
      event.returnValue = '';
    }
  });
  window.addEventListener('resize', () => { $('#job-options').hidden = true; });
  wrap.addEventListener('scroll', () => { $('#job-options').hidden = true; });

  $('#loading').hidden = true;
  wrap.hidden = false;
  $('#savebar').hidden = false;
  $('#preview-btn').hidden = !state.viewAs; // engineers just Save; the office can preview the journal lines
  const params = new URLSearchParams(location.search);
  const requested = params.get('month');
  if (state.viewAs) showViewing();
  await openMonth(isMonth(requested) ? requested : thisMonth());
  if (state.viewAs) return;
  const source = params.get('copyFrom');
  if (isMonth(source) && source !== state.month) await copyRows(source, { includeClosed: params.get('closed') === '1' });
}

function isClosed() {
  return (state.bundle?.closedMonths || []).includes(state.month);
}

// Closed months, and anyone else's timesheet, can be looked at but not changed.
function isReadOnly() {
  return Boolean(state.viewAs) || isClosed();
}

function showViewing() {
  const { person } = state.viewAs;
  const first = person.name.split(' ')[0];
  document.body.classList.add('viewing-as');
  const back = document.querySelector('.user a[href="home.html"]');
  if (back) {
    back.href = `home.html?as=${encodeURIComponent(person.username)}`;
    back.textContent = `${first}’s timesheets`;
  }
  setNotice('viewing', {
    tone: 'view',
    title: `Viewing ${person.name}’s timesheet, read-only.`,
    text: 'This is their last save for the month, as they see it. Hours they haven’t saved yet are only on their own computer.',
    action: { label: 'Back to Admin', run: () => { location.href = 'admin.html'; } },
  });
}

if (session) start();
