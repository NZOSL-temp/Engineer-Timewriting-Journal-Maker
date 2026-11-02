// Admin › Timesheets from spreadsheets. The office drops in the timesheet spreadsheets that
// engineers have sent, any number at once, and each one fills in that engineer's month as if
// they'd loaded it on their own timesheet page and pressed Save.
//
// Each file is read here in the browser (see xlsx.js) and never uploaded. Who it's for comes
// from its NAME or its file name, and its month from its dates; both can be changed. Nothing
// is saved until the office has pressed "Allow write access" (kept for this tab, until Stop
// editing) and then Save, which says first which saved months it replaces. A month an
// engineer saves again in the meantime isn't replaced without asking again. Saving uses the
// engineer's keys, which only admins' sign-ins carry.

import {
  $, el, toast, TASKS, cleanText, monthInfo, shiftMonth, isMonth, fmtHours, fmtDateTime, askConfirm, writerFor,
  saveToGitHub, readVerified, savedMonths, defaultUsername, FIRST_MONTH, latestMonth, monthSpan, sheetsWriteAccess,
} from './app.js';
import { applyChoices, readFirstSheet, sheetMonth, sheetName, isJobReport, timesheetFromSheet } from './xlsx.js';
import { fireworks } from './fireworks.js';

const MAX_BYTES = 25 * 1024 * 1024;
let page = null; // { bundle, profile, repo, settings(), onSaved(since) } from admin.js
let jobs = new Map();
const rows = []; // one per spreadsheet, in the order they were added
const saves = new Map(); // username -> { status: 'loading' | 'ok' | 'failed', months: Map(month -> their save) }
let verified = null; // the verified copies of everyone's timesheets, read once
let saving = false;
let ids = 0;

// ----------------------------------------------------------- who it's for

// The engineers who can sign in (timesheets can't be saved for ones who've left), by name.
function engineers() {
  return (page.settings()?.employees || []).filter((e) => e.active !== false)
    .map((e) => ({ name: e.name, username: e.username || defaultUsername(e.name) }))
    .sort((a, b) => a.name.localeCompare(b.name));
}
const engineer = (username) => engineers().find((p) => p.username === username) || null;
const firstName = (person) => person.name.split(' ')[0];

// Words common in timesheet file names, which shouldn't be taken for someone's name.
const NOT_NAMES = new Set(['eng', 'engineer', 'engineers', 'engineering', 'time', 'timesheet', 'timesheets', 'sheet', 'sheets',
  'timewriting', 'writing', 'proforma', 'template', 'copy', 'of', 'final', 'draft', 'new', 'month', 'monthly', 'hours', 'for',
  'and', 'the', 'test', 'xlsx', 'xlsm', 'jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'sept', 'oct', 'nov',
  'dec', 'january', 'february', 'march', 'april', 'june', 'july', 'august', 'september', 'october', 'november', 'december']);
const words = (text) => String(text || '').normalize('NFKD').replace(/[̀-ͯ]/g, '').toLowerCase()
  .split(/[^a-z]+/).filter(Boolean);

// How well `tokens` (the words of a NAME cell or a file name) fit someone: their full name,
// first name and surname initial ("Ann E."), first name, or (an unusual) surname.
function fit(person, tokens) {
  const parts = words(person.name);
  if (!parts.length) return 0;
  const first = parts[0];
  const last = parts[parts.length - 1];
  if (parts.length > 1 && parts.every((part) => tokens.includes(part))) return 4;
  if (parts.length > 1 && tokens.some((t, i) => t === first && tokens[i + 1] === last[0])) return 3;
  if (!NOT_NAMES.has(first) && tokens.includes(first)) return 2;
  if (parts.length > 1 && last.length > 1 && !NOT_NAMES.has(last) && tokens.includes(last)) return 1;
  return 0;
}

// The one engineer `text` points to, or null (no one, or more than one equally).
export function pointsTo(text, people) {
  const tokens = words(text);
  const scored = people.map((person) => ({ person, score: fit(person, tokens) })).filter((s) => s.score > 0);
  const best = Math.max(0, ...scored.map((s) => s.score));
  const top = scored.filter((s) => s.score === best);
  return top.length === 1 ? top[0].person : null;
}

// Who a spreadsheet is for, from its NAME and its file name: { person } or { why } it's unclear.
export function whoFor(nameCell, fileName, people) {
  const byName = nameCell ? pointsTo(nameCell, people) : null;
  const byFile = pointsTo(fileName.replace(/\.[^.]*$/, ''), people);
  if (byName && byFile && byName.username !== byFile.username) {
    return { why: `its NAME is “${nameCell}”, but its file name points to ${byFile.name}` };
  }
  return byName || byFile ? { person: byName || byFile } : { why: '' };
}

// --------------------------------------------------------------- reading

async function addFiles(fileList) {
  const files = [...(fileList || [])];
  if (!files.length) return;
  if (saving) {
    toast('Wait until the timesheets have saved, then add more.', { tone: 'warn' });
    return;
  }
  const added = [];
  for (const file of files) {
    if (rows.some((r) => r.file.name === file.name && r.file.size === file.size && r.file.modified === file.lastModified)) {
      toast(`${file.name} is already in the list.`);
      continue;
    }
    const row = { id: `s${ids += 1}`, file: { name: file.name, size: file.size, modified: file.lastModified }, state: 'reading', notes: [] };
    rows.push(row);
    added.push([row, file]);
  }
  refresh();
  for (const [row, file] of added) {
    try {
      if (!/\.xls[xm]$/i.test(file.name)) throw new Error('This isn’t an Excel workbook (.xlsx). In Excel, use File › Save As › Excel Workbook (.xlsx).');
      if (file.size > MAX_BYTES) throw new Error('This file is too big to be a timesheet (over 25 MB).');
      const sheet = await readFirstSheet(new Uint8Array(await file.arrayBuffer()));
      if (isJobReport(sheet)) throw new Error('This is the Job Task Overview report, not a timesheet: upload it under Job list, at the top of this page.');
      row.sheet = sheet;
      row.nameCell = sheetName(sheet);
      row.sheetMonth = sheetMonth(sheet);
      row.month = row.sheetMonth;
      const guess = whoFor(row.nameCell, file.name, engineers());
      row.username = guess.person?.username || '';
      row.why = guess.why || '';
    } catch (error) {
      row.readError = error.message || 'This spreadsheet couldn’t be read.';
    }
    row.state = 'checking';
    refresh();
  }
}

// What's already saved for an engineer, fetched once (in the background) when it's needed.
function loadSaves(person) {
  const entry = { status: 'loading', months: new Map() };
  saves.set(person.username, entry);
  (async () => {
    try {
      verified ||= readVerified(page.repo);
      entry.months = await savedMonths(page.bundle, page.profile, person, await verified);
      entry.status = 'ok';
    } catch {
      verified = null;
      entry.status = 'failed';
    }
    refresh();
  })();
}

// ---------------------------------------------------------- checking each

const positive = (hours) => {
  const out = {};
  for (const [day, value] of Object.entries(hours || {})) {
    const h = Math.round(Number(value) * 100) / 100;
    if (h > 0) out[day] = h;
  }
  return out;
};
const sum = (hours) => Object.values(hours || {}).reduce((a, b) => a + Number(b), 0);

// The month as it's saved (the same shape and tidying as the timesheet page's Save).
function toRecord(data) {
  return {
    jobs: (data?.jobs || []).map((job) => {
      const hours = {};
      for (const task of TASKS) {
        const h = positive(job.hours?.[task]);
        if (Object.keys(h).length) hours[task] = h;
      }
      return { job: job.job, location: cleanText(job.location), owner: cleanText(job.owner), description: cleanText(job.description), hours };
    }),
    other: (data?.other || []).map((row) => ({ label: cleanText(row.label), hours: positive(row.hours) })),
  };
}

const totalHours = (record) => record.jobs.reduce((a, job) => a + sum(job.hours.CAPEX) + sum(job.hours.REVEX), 0)
  + record.other.reduce((a, row) => a + sum(row.hours), 0);

function dayTotals(record) {
  const totals = {};
  const add = (hours) => { for (const [day, h] of Object.entries(hours)) totals[day] = Math.round(((totals[day] || 0) + h) * 100) / 100; };
  record.jobs.forEach((job) => Object.values(job.hours).forEach(add));
  record.other.forEach((row) => add(row.hours));
  return totals;
}

// Whether two months hold the same rows and hours (so saving would change nothing).
function sameHours(a, b) {
  const byDay = (hours) => Object.entries(positive(hours)).sort((x, y) => Number(x[0]) - Number(y[0]));
  const key = (data) => JSON.stringify([
    (data?.jobs || []).map((job) => JSON.stringify([job.job, TASKS.map((task) => byDay(job.hours?.[task]))])).sort(),
    (data?.other || []).map((row) => JSON.stringify([cleanText(row.label), byDay(row.hours)])).sort(),
  ]);
  return key(a) === key(b);
}

// Lines on jobs that can't take time (the timesheet page marks these; here they're listed).
function jobNotes(record) {
  const lines = [];
  for (const job of record.jobs) {
    const info = jobs.get(job.job);
    for (const task of TASKS) {
      const hours = sum(job.hours[task]);
      if (!hours) continue;
      if (!info) lines.push(`${job.job} ${task} (${fmtHours(hours)} h) isn’t open in the latest job list`);
      else if (!info.tasks.includes(task)) lines.push(`${job.job} ${task} (${fmtHours(hours)} h): the job is only open for ${info.tasks.join(' and ')}`);
    }
  }
  if (!lines.length) return [];
  return [`${lines.length === 1 ? 'A line is' : `${lines.length} lines are`} on jobs that can’t take time (closed, or still at Quote or Planning): `
    + `${lines.join('; ')}. The journal’s checks list ${lines.length === 1 ? 'it' : 'them'} too.`];
}

const closedMonths = () => page.settings()?.closedMonths || page.bundle.closedMonths || [];

// Work out what saving a row would do: its state is 'choose' (who or which month is needed),
// 'blocked' (it can't be saved, with the problem), 'checking' (looking up what's saved),
// 'same' (nothing would change) or 'ready'.
function evaluate(row) {
  if (['reading', 'saving', 'saved'].includes(row.state)) return;
  Object.assign(row, { state: 'blocked', problem: '', notes: [], record: null, existing: null, choices: [] });
  const stop = (problem, state = 'blocked') => Object.assign(row, { state, problem });
  if (row.readError) return stop(row.readError);
  const person = engineer(row.username);
  if (!person) return stop(row.why ? `Choose who it’s for: ${row.why}.` : 'Choose who it’s for: its NAME and file name don’t say.', 'choose');
  if (!row.month) return stop('Choose its month: there are no dates in the spreadsheet.', 'choose');
  const label = monthInfo(row.month).label;
  if (row.month < FIRST_MONTH || row.month > latestMonth()) return stop(`It’s for ${label}, but only ${monthSpan()} can be loaded.`);
  if (closedMonths().includes(row.month)) return stop(`${label} is closed. To load it, Reopen ${label} under Journals first.`);
  if (!page.profile.writeKeys?.[person.username]) {
    return stop(`The website doesn’t have ${person.name}’s key yet (they were added recently). Reload this page in a minute.`);
  }
  let result;
  try {
    result = timesheetFromSheet(row.sheet, row.month, { jobs, otherTime: page.bundle.otherTime || [] });
  } catch (error) {
    return stop(isMonth(error.month) ? `${error.message} Choose ${monthInfo(error.month).label} as its month.` : error.message);
  }
  // Jobs open for both CAPEX and REVEX that the spreadsheet doesn't say: chosen in the list.
  row.picks ||= {};
  row.choices = result.choices || [];
  const record = toRecord(applyChoices(result.data, row.choices, row.picks));
  row.record = record;
  if (!record.jobs.length && !record.other.length) return stop(`It has no jobs or hours for ${label}.`);
  if (record.jobs.length > 200 || record.other.length > 100) return stop('It has more rows than a timesheet can hold (200 jobs and 100 other-time rows).');
  const everyHour = toRecord(applyChoices(result.data, row.choices, Object.fromEntries(row.choices.map((c) => [c.job, 'REVEX']))));
  const over = Object.entries(dayTotals(everyHour)).filter(([, h]) => h > 24).map(([day, h]) => `${day} ${label.slice(0, 3)}: ${fmtHours(h)} h`);
  if (over.length) {
    return stop(`More than 24 hours on ${over.length === 1 ? 'a day' : `${over.length} days`} (${over.join('; ')}). Fix the spreadsheet, then add it again.`);
  }
  const twin = rows.find((r) => r !== row && r.username === row.username && r.month === row.month && r.sheet && r.state !== 'saved');
  if (twin) return stop(`${twin.file.name} is for ${person.name}’s ${label} too. Remove one of them.`);
  const unpicked = row.choices.filter((c) => !TASKS.includes(row.picks[c.job]));
  if (unpicked.length) {
    return stop(`Choose CAPEX or REVEX for ${unpicked.map((c) => c.job).join(', ')} below: ${unpicked.length === 1 ? 'it’s' : 'they’re'} open for both, `
      + 'and the spreadsheet doesn’t say.', 'choose');
  }

  row.notes = [...result.notes, ...jobNotes(record)];
  if (row.nameCell && pointsTo(row.nameCell, engineers())?.username !== person.username) {
    row.notes.unshift(`Its NAME is “${row.nameCell}”, not ${person.name}: check it’s the right spreadsheet.`);
  }
  const known = saves.get(person.username);
  if (!known) loadSaves(person);
  if (!known || known.status === 'loading') return Object.assign(row, { state: 'checking' });
  if (known.status === 'failed') {
    row.notes.push(`Couldn’t check whether ${firstName(person)} has already saved ${label}. If they have, Save tells you and doesn’t replace it.`);
  }
  row.existing = known.months.get(row.month) || null;
  return Object.assign(row, { state: row.existing && sameHours(row.existing.data, record) ? 'same' : 'ready' });
}

function refresh() {
  rows.forEach(evaluate);
  render();
}

// ---------------------------------------------------------------- saving

async function allowWriting() {
  const yes = await askConfirm('Allow write access to save these spreadsheets?',
    'Save will then store each spreadsheet in this list as that engineer’s timesheet for its month: they’ll see it, and the journal uses it. '
    + 'You’re still asked before anything is saved, with the saved months it replaces. It lasts until you press Stop editing or close this tab.',
    'Allow write access');
  if (!yes) return;
  sheetsWriteAccess.allow();
  render();
  $('#sheets-save')?.focus();
}

function stopWriting() {
  sheetsWriteAccess.stop();
  render();
}

async function saveAll() {
  if (saving || !sheetsWriteAccess.has()) return;
  const ready = rows.filter((r) => r.state === 'ready');
  if (!ready.length) return;
  const replacing = ready.filter((r) => r.existing);
  const count = `${ready.length} timesheet${ready.length === 1 ? '' : 's'}`;
  const whose = (r) => `${engineer(r.username).name}’s ${monthInfo(r.month).label}`;
  const ok = await askConfirm(`Save ${count}?`, `Each spreadsheet is saved as that engineer’s timesheet for its month, as if they’d loaded it `
    + 'and pressed Save: they’ll see it, and the journal uses it.'
    + (replacing.length ? ` It replaces ${replacing.map(whose).join(', ')}, which ${replacing.length === 1 ? 'is' : 'are'} already saved: `
      + `everything in ${replacing.length === 1 ? 'it' : 'them'} is replaced by what’s in the spreadsheet${replacing.length === 1 ? '' : 's'}, `
      + 'and days left empty there end up empty.' : ''), `Save ${count}`);
  if (!ok || saving) return;
  saving = true;
  const since = new Date(Date.now() - 5000).toISOString().replace(/\.\d+Z$/, 'Z');
  let done = 0;
  for (const row of ready) {
    if (row.state !== 'ready') continue;
    const person = engineer(row.username);
    row.state = 'saving';
    row.message = null;
    render();
    const data = { v: 1, employee: person.name, month: row.month, savedAt: new Date().toISOString(), ...row.record };
    const theirs = saves.get(person.username)?.months;
    try {
      await saveToGitHub(page.bundle, writerFor(page.profile, person), person.username, data, { replacing: row.existing?.savedAt || '' });
      Object.assign(row, { state: 'saved', savedAt: data.savedAt });
      theirs?.set(row.month, { month: row.month, savedAt: data.savedAt, data });
      done += 1;
    } catch (error) {
      row.state = 'checking'; // worked out again below
      if (error.code === 'newer') {
        theirs?.set(row.month, { month: row.month, savedAt: error.saved.savedAt, data: error.saved });
        row.message = { tone: 'warn', text: `${firstName(person)} saved ${monthInfo(row.month).label} at ${fmtDateTime(error.saved.savedAt)}, `
          + 'after this list was made, so it wasn’t replaced. Press Save again to replace it.' };
      } else {
        row.message = { tone: 'error', text: `Not saved: ${error.message || 'something went wrong.'}` };
      }
    }
    refresh();
  }
  saving = false;
  refresh();
  if (done) {
    toast(`Saved ${done} timesheet${done === 1 ? '' : 's'}. The journals update in about a minute.`, { tone: 'ok', duration: 7000 });
    fireworks();
    page.onSaved(since);
  }
}

// ------------------------------------------------------------- the list

function monthOptions(row) {
  const months = [];
  for (let m = latestMonth(); m >= FIRST_MONTH; m = shiftMonth(m, -1)) months.push(m);
  if (isMonth(row.sheetMonth) && !months.includes(row.sheetMonth)) months.push(row.sheetMonth);
  const closed = closedMonths();
  return months.map((m) => el('option', { value: m, selected: m === row.month }, `${monthInfo(m).label}${closed.includes(m) ? ' (closed)' : ''}`));
}

const STATUS = {
  reading: ['muted', 'Reading…'], checking: ['muted', 'Checking…'], choose: ['warn', 'Choose'], blocked: ['error', 'Can’t load'],
  same: ['muted', 'No change'], saving: ['muted', 'Saving…'], saved: ['ok', 'Saved'],
};

function statusCell(row, person) {
  const label = row.month ? monthInfo(row.month).label : '';
  let [tone, chip] = STATUS[row.state] || ['ok', 'New'];
  let text = row.problem;
  if (row.state === 'ready' && row.existing) {
    [tone, chip] = ['warn', 'Replaces'];
    text = `${firstName(person)}’s save of ${fmtDateTime(row.existing.savedAt)} (${fmtHours(totalHours(toRecord(row.existing.data)))} h).`;
  } else if (row.state === 'ready') text = `A new timesheet for ${firstName(person)}.`;
  else if (row.state === 'same') text = `Already saved like this (${fmtDateTime(row.existing.savedAt)}).`;
  else if (row.state === 'saved') text = `As ${firstName(person)}’s ${label}, ${fmtDateTime(row.savedAt)}.`;
  return el('td', { class: 'bulk-status' },
    el('span', { class: `chip chip-${tone}` }, chip),
    text ? el('span', { class: 'row-note' }, text) : null,
    row.message && row.state === 'ready' ? el('span', { class: `row-note bulk-message ${row.message.tone}` }, row.message.text) : null);
}

function whatsInIt(record) {
  const items = [
    ...record.jobs.flatMap((job) => {
      const tasks = TASKS.filter((task) => sum(job.hours[task]));
      return (tasks.length ? tasks : ['']).map((task) => el('li', {},
        el('strong', {}, job.job), task ? ` ${task}` : '', ` · ${fmtHours(sum(job.hours[task]))} h`,
        job.description ? el('span', { class: 'muted' }, ` · ${job.description}`) : null));
    }),
    ...record.other.map((row) => el('li', {}, el('strong', {}, row.label), ` · ${fmtHours(sum(row.hours))} h`)),
  ];
  return el('ul', { class: 'bulk-what' }, items);
}

function rowLines(row, people) {
  const person = engineer(row.username);
  const locked = saving || ['reading', 'saving', 'saved'].includes(row.state);
  const choose = (field) => (event) => {
    row[field] = event.currentTarget.value;
    row.message = null;
    if (field === 'username') row.why = '';
    refresh();
  };
  const who = el('select', { class: 'reuse-select', disabled: locked || Boolean(row.readError), 'aria-label': `Who ${row.file.name} is for`,
    dataset: { focus: `${row.id}-who` }, onchange: choose('username') },
  el('option', { value: '' }, 'Choose…'), people.map((p) => el('option', { value: p.username, selected: p.username === row.username }, p.name)));
  const when = el('select', { class: 'reuse-select', disabled: locked || Boolean(row.readError), 'aria-label': `The month ${row.file.name} is for`,
    dataset: { focus: `${row.id}-month` }, onchange: choose('month') },
  el('option', { value: '' }, 'Choose…'), monthOptions(row));
  const action = row.state === 'saved'
    ? el('a', { class: 'btn btn-sm', href: `timesheet.html?month=${row.month}&as=${encodeURIComponent(row.username)}`,
      title: `See ${person?.name || 'their'} ${monthInfo(row.month).label} as they do` }, 'Open')
    : el('button', { class: 'btn btn-sm btn-ghost', type: 'button', disabled: saving,
      'aria-label': `Remove ${row.file.name} from the list`, dataset: { focus: `${row.id}-remove` },
      onclick: () => { rows.splice(rows.indexOf(row), 1); refresh(); } }, 'Remove');
  const lines = [el('tr', {},
    el('th', { scope: 'row' }, el('span', { class: 'bulk-file' }, row.file.name),
      row.nameCell ? el('span', { class: 'row-note' }, `NAME: ${row.nameCell}`) : null),
    el('td', {}, who),
    el('td', {}, when),
    el('td', { class: 'num' }, row.record ? fmtHours(totalHours(row.record) + (row.choices || []).filter((c) => !TASKS.includes(row.picks?.[c.job])).reduce((a, c) => a + sum(c.hours), 0)) : '–',
      row.record ? el('span', { class: 'row-note' }, `${row.record.jobs.length} job${row.record.jobs.length === 1 ? '' : 's'}`) : null),
    statusCell(row, person),
    el('td', { class: 'row-actions' }, action))];
  const showNotes = row.notes?.length && ['ready', 'same', 'saving', 'saved'].includes(row.state);
  const picking = row.choices?.length && !['saving', 'saved'].includes(row.state) ? el('div', { class: 'checks checks-warn bulk-picks' },
    el('strong', {}, 'CAPEX or REVEX?'),
    el('span', { class: 'muted' }, ' These jobs are open for both in the Job Task Overview report, and the spreadsheet doesn’t say which.'),
    el('ul', {}, row.choices.map((choice) => el('li', {},
      el('label', {}, `${choice.job}${choice.description ? ` ${choice.description}` : ''} (${fmtHours(sum(choice.hours))} h) `,
        el('select', { class: 'reuse-select', disabled: saving, 'aria-label': `CAPEX or REVEX for ${choice.job}`,
          dataset: { focus: `${row.id}-${choice.job}` },
          onchange: (event) => { row.picks[choice.job] = event.currentTarget.value; row.message = null; refresh(); } },
        el('option', { value: '' }, 'Choose…'),
        TASKS.map((task) => el('option', { value: task, selected: row.picks?.[choice.job] === task }, task)))))))) : null;
  if (showNotes || row.record || picking) {
    lines.push(el('tr', { class: 'bulk-more' }, el('td', { colspan: 6 }, picking,
      showNotes ? el('div', { class: 'checks checks-warn' }, el('strong', {}, 'Check:'), el('ul', {}, row.notes.map((n) => el('li', {}, n)))) : null,
      row.record ? el('details', { class: 'bulk-details', open: Boolean(row.open), ontoggle: (e) => { row.open = e.currentTarget.open; } },
        el('summary', {}, 'What’s in it'), whatsInIt(row.record)) : null)));
  }
  return lines;
}

function render() {
  const box = $('#sheet-list');
  if (!rows.length) {
    box.replaceChildren();
    return;
  }
  const focused = document.activeElement?.dataset?.focus;
  const people = engineers();
  const count = (state) => rows.filter((r) => r.state === state).length;
  const ready = count('ready');
  const parts = [
    ready && `${ready} to save`, count('choose') && `${count('choose')} to choose`, count('blocked') && `${count('blocked')} can’t be loaded`,
    count('same') && `${count('same')} unchanged`, count('saved') && `${count('saved')} saved`,
    (count('reading') + count('checking')) && 'checking the rest…',
  ].filter(Boolean);
  box.replaceChildren(
    el('div', { class: 'bulk-scroll' }, el('table', { class: 'sheets bulk-table' },
      el('thead', {}, el('tr', {},
        el('th', { scope: 'col' }, 'Spreadsheet'), el('th', { scope: 'col' }, 'Engineer'), el('th', { scope: 'col' }, 'Month'),
        el('th', { scope: 'col', class: 'num' }, 'Hours'), el('th', { scope: 'col' }, 'Status'),
        el('th', { scope: 'col' }, el('span', { class: 'sr-only' }, 'Actions')))),
      rows.map((row) => el('tbody', { class: 'bulk-row', dataset: { state: row.state } }, rowLines(row, people))))),
    el('div', { class: 'bulk-bar' },
      el('span', { class: 'bulk-count' }, parts.join(' · ')),
      ...(sheetsWriteAccess.has() || saving ? [
        el('button', { id: 'sheets-save', class: 'btn btn-primary', type: 'button', disabled: saving || !ready, onclick: saveAll },
          saving ? 'Saving…' : `Save ${ready ? `${ready} ` : ''}timesheet${ready === 1 ? '' : 's'}`),
        el('button', { id: 'sheets-stop', class: 'btn', type: 'button', disabled: saving, onclick: stopWriting,
          title: 'Saving needs Allow write access again' }, 'Stop editing'),
      ] : [
        el('button', { id: 'sheets-allow', class: `btn${ready ? ' btn-primary' : ''}`, type: 'button', onclick: allowWriting,
          title: 'Needed before these can be saved as the engineers’ timesheets' }, 'Allow write access'),
      ]),
      el('button', { class: 'link-btn', type: 'button', disabled: saving, onclick: () => { rows.length = 0; render(); } }, 'Clear the list')));
  if (focused) box.querySelector(`[data-focus="${focused}"]:not(:disabled)`)?.focus();
}

// --------------------------------------------------------------- set up

// When the settings change on the Admin page (a month closed or reopened, engineers added or
// removed), the list is worked out again.
export function settingsChanged() {
  if (page && !saving) refresh();
}

export function setUpBulkLoad(context) {
  page = context;
  jobs = new Map((page.bundle.jobs || []).map((job) => [job.no, job]));
  const zone = $('#sheet-drop');
  const input = $('#sheet-files');
  zone.classList.remove('disabled');
  input.disabled = false;
  input.addEventListener('change', () => {
    addFiles(input.files);
    input.value = '';
  });
  zone.addEventListener('dragover', (event) => {
    event.preventDefault();
    zone.classList.add('over');
  });
  zone.addEventListener('dragleave', () => zone.classList.remove('over'));
  zone.addEventListener('drop', (event) => {
    event.preventDefault();
    zone.classList.remove('over');
    addFiles(event.dataTransfer.files);
  });
}
