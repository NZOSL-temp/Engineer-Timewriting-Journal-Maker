// Admin page: upload a new Job Task Overview report into the private data repo
// (which updates the website's job list), and view or download the journals.
// Everything here uses the admin token from the person's profile. Every file in the data
// repo is encrypted (see adminRepo in app.js), and commit messages name nobody.

import {
  $, el, toast, loadSession, clearSession, openSession, adminRepo, b64ToBytes, bytesToB64, journalTable, monthInfo,
  cleanText, nameProblem, nameKey, defaultUsername, issuedPassword,
} from './app.js';
import { fireworks } from './fireworks.js';

const session = loadSession();
if (!session?.uk) location.replace('index.html?next=admin.html');

const REPORT_PATH = 'input/report.json';
const JOBS = 'generated/jobs.json';
const REQUIRED = ['Project No.', 'Job Description', 'Job Location', 'Owner', 'Project Task No.', 'Status'];
const state = { bundle: null, profile: null, repo: null, uploading: false, settings: null, settingsSha: null, users: {}, busy: false };
const SETTINGS = 'settings.json';
const USERS = 'users.json';

function notice(tone, text) {
  $('#notices').replaceChildren(el('div', { class: `notice notice-${tone}` }, el('div', { class: 'notice-text' }, text)));
}

// ------------------------------------------------------------- the report

// Read one file out of the .xlsx (a zip) so we can check it's the right report.
async function zipText(bytes, wanted) {
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  let end = -1;
  for (let i = bytes.length - 22; i >= Math.max(0, bytes.length - 65557); i -= 1) {
    if (view.getUint32(i, true) === 0x06054b50) {
      end = i;
      break;
    }
  }
  if (end < 0) throw new Error('not a zip');
  const count = view.getUint16(end + 10, true);
  let at = view.getUint32(end + 16, true);
  const names = new TextDecoder();
  for (let n = 0; n < count; n += 1) {
    if (view.getUint32(at, true) !== 0x02014b50) throw new Error('not a zip');
    const method = view.getUint16(at + 10, true);
    const size = view.getUint32(at + 20, true);
    const nameLength = view.getUint16(at + 28, true);
    const skip = nameLength + view.getUint16(at + 30, true) + view.getUint16(at + 32, true);
    const local = view.getUint32(at + 42, true);
    if (names.decode(bytes.subarray(at + 46, at + 46 + nameLength)) === wanted) {
      const start = local + 30 + view.getUint16(local + 26, true) + view.getUint16(local + 28, true);
      const data = bytes.subarray(start, start + size);
      if (method === 0) return new TextDecoder().decode(data);
      if (method === 8) return new Response(new Blob([data]).stream().pipeThrough(new DecompressionStream('deflate-raw'))).text();
      return null;
    }
    at += 46 + skip;
  }
  return null;
}

const unescapeXml = (text) => text.replace(/&(amp|lt|gt|quot|apos|#\d+);/g, (m, e) => (
  { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'" }[e] ?? String.fromCharCode(Number(e.slice(1)))));

// The report's column headings that are missing, or null if this browser can't check.
async function missingColumns(bytes) {
  if (bytes[0] !== 0x50 || bytes[1] !== 0x4b) throw new Error('not a zip');
  if (typeof DecompressionStream !== 'function') return null;
  const text = (await zipText(bytes, 'xl/sharedStrings.xml')) ?? (await zipText(bytes, 'xl/worksheets/sheet1.xml'));
  if (text == null) return null;
  const found = new Set([...text.matchAll(/<t(?:\s[^>]*)?>([^<]*)<\/t>/g)].map((m) => unescapeXml(m[1]).replace(/\s+/g, ' ').trim().toLowerCase()));
  return REQUIRED.filter((name) => !found.has(name.toLowerCase()));
}

function step(id, text, tone = 'busy') {
  const list = $('#upload-steps');
  list.hidden = false;
  let item = list.querySelector(`[data-step="${id}"]`);
  if (!item) {
    item = el('li', { dataset: { step: id } });
    list.append(item);
  }
  item.dataset.tone = tone;
  item.replaceChildren(el('span', { class: 'step-icon', 'aria-hidden': 'true' }, { busy: '', ok: '✓', error: '!' }[tone]), el('span', {}, text));
}

const sleep = (ms) => new Promise((resolve) => { setTimeout(resolve, ms); });

// To spot an upload of the same report again.
async function sha256Hex(bytes) {
  return [...new Uint8Array(await crypto.subtle.digest('SHA-256', bytes))].map((b) => b.toString(16).padStart(2, '0')).join('');
}

// Wait for the "Update website and journals" run started by our commit.
async function waitForPublish(sha) {
  const deadline = Date.now() + 8 * 60 * 1000;
  while (Date.now() < deadline) {
    await sleep(5000);
    const { workflow_runs: runs = [] } = await state.repo.runs(sha);
    const run = runs.find((r) => (r.path || '').endsWith('update.yml'));
    if (run?.status === 'completed') return run;
  }
  return null;
}

async function jobsSummary() {
  try {
    const { data } = await state.repo.readJson(JOBS);
    const rows = data.stats?.rows ? ` (from ${data.stats.rows.toLocaleString('en-NZ')} job task rows in the report)` : '';
    return `The website now has ${data.jobs.length.toLocaleString('en-NZ')} open jobs${rows}. Engineers see them after reloading the page, within a couple of minutes.`;
  } catch {
    return 'The website’s job list has been updated. Engineers see it after reloading the page, within a couple of minutes.';
  }
}

async function upload(file) {
  if (state.uploading || !file) return;
  state.uploading = true;
  $('#dropzone').classList.add('disabled');
  $('#upload-steps').replaceChildren();
  let current = 'check';
  try {
    step('check', `Checking ${file.name}…`);
    if (!/\.xlsx$/i.test(file.name)) throw new Error('That isn’t an .xlsx file. Export the Job Task Overview report from Business Central to Excel.');
    if (file.size > 25 * 1024 * 1024) throw new Error('That file is over 25 MB, which is far too big for a Job Task Overview report.');
    const bytes = new Uint8Array(await file.arrayBuffer());
    let missing;
    try {
      missing = await missingColumns(bytes);
    } catch {
      throw new Error('That file can’t be opened as an Excel .xlsx file.');
    }
    if (missing?.length) {
      throw new Error(`This doesn’t look like the Job Task Overview report: it has no ${missing.map((m) => `“${m}”`).join(', ')} column${missing.length > 1 ? 's' : ''}.`);
    }
    step('check', `${file.name} looks like a Job Task Overview report.`, 'ok');

    current = 'upload';
    step('upload', 'Saving it in the private repo…');
    const sha256 = await sha256Hex(bytes);
    const known = await state.repo.readJson(JOBS).catch(() => null);
    if (known?.data?.sha256 === sha256) {
      step('upload', 'This is the same report as the current one, so the job list is already up to date.', 'ok');
      return;
    }
    const existing = await state.repo.file(REPORT_PATH);
    const report = { name: file.name, sha256, uploaded: new Date().toLocaleDateString('en-CA'), xlsx: bytesToB64(bytes) };
    const result = await state.repo.write(REPORT_PATH, new TextEncoder().encode(JSON.stringify(report)),
      'Upload a new job report (admin page)', existing?.sha);
    step('upload', 'Saved in the private repo.', 'ok');

    current = 'publish';
    step('publish', 'Updating the website’s job list. This takes about a minute…');
    const run = await waitForPublish(result.commit);
    if (!run) throw new Error('This is taking longer than usual. Check back in a few minutes; the job list updates on its own.');
    if (run.conclusion !== 'success') {
      throw new Error('The report was saved, but the website couldn’t read it. Check it’s the Job Task Overview export, or look at the “Update website and journals” run in the data repo’s Actions tab.');
    }
    step('publish', await jobsSummary(), 'ok');
    $('#jobs-current').textContent = `Current report: “${file.name}”, uploaded just now.`;
    toast('The job list has been updated.', { tone: 'ok' });
  } catch (error) {
    step(current, error.message, 'error');
  } finally {
    state.uploading = false;
    $('#dropzone').classList.remove('disabled');
    $('#report-file').value = '';
  }
}

function setUpUpload() {
  const zone = $('#dropzone');
  $('#report-file').addEventListener('change', (event) => upload(event.target.files[0]));
  zone.addEventListener('dragover', (event) => {
    event.preventDefault();
    zone.classList.add('over');
  });
  zone.addEventListener('dragleave', () => zone.classList.remove('over'));
  zone.addEventListener('drop', (event) => {
    event.preventDefault();
    zone.classList.remove('over');
    upload(event.dataTransfer.files[0]);
  });
}

// --------------------------------------------------------------- journals

function parseCsv(text) {
  const rows = [];
  let row = [];
  let field = '';
  let quoted = false;
  const input = text.replace(/^﻿/, '');
  for (let i = 0; i < input.length; i += 1) {
    const c = input[i];
    if (quoted) {
      if (c !== '"') field += c;
      else if (input[i + 1] === '"') {
        field += '"';
        i += 1;
      } else quoted = false;
    } else if (c === '"') quoted = true;
    else if (c === ',') {
      row.push(field);
      field = '';
    } else if (c === '\n' || c === '\r') {
      if (c === '\r' && input[i + 1] === '\n') i += 1;
      row.push(field);
      rows.push(row);
      row = [];
      field = '';
    } else field += c;
  }
  if (field || row.length) rows.push([...row, field]);
  return rows;
}

const money = (text) => (text === '' ? '' : Number(text).toLocaleString('en-NZ', { minimumFractionDigits: 2, maximumFractionDigits: 2 }));

function saveBlob(blob, name) {
  const url = URL.createObjectURL(blob);
  const link = el('a', { href: url, download: name });
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
}

// A .zip holding several files (stored, not compressed: .xlsx files are compressed already).
const CRC_TABLE = Array.from({ length: 256 }, (_, n) => {
  let c = n;
  for (let k = 0; k < 8; k += 1) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
  return c >>> 0;
});
function crc32(bytes) {
  let c = 0xffffffff;
  for (const b of bytes) c = CRC_TABLE[(c ^ b) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}
function makeZip(files) {
  const parts = [];
  const central = [];
  let offset = 0;
  const now = new Date();
  const time = (now.getHours() << 11) | (now.getMinutes() << 5) | (now.getSeconds() >> 1);
  const date = ((now.getFullYear() - 1980) << 9) | ((now.getMonth() + 1) << 5) | now.getDate();
  for (const { name, data } of files) {
    const nameBytes = new TextEncoder().encode(name);
    const crc = crc32(data);
    const local = new DataView(new ArrayBuffer(30));
    [[0, 0x04034b50, 4], [4, 20, 2], [6, 0x0800, 2], [8, 0, 2], [10, time, 2], [12, date, 2], [14, crc, 4],
      [18, data.length, 4], [22, data.length, 4], [26, nameBytes.length, 2], [28, 0, 2]]
      .forEach(([at, value, size]) => (size === 4 ? local.setUint32(at, value, true) : local.setUint16(at, value, true)));
    const entry = new DataView(new ArrayBuffer(46));
    [[0, 0x02014b50, 4], [4, 20, 2], [6, 20, 2], [8, 0x0800, 2], [10, 0, 2], [12, time, 2], [14, date, 2], [16, crc, 4],
      [20, data.length, 4], [24, data.length, 4], [28, nameBytes.length, 2], [30, 0, 2], [32, 0, 2], [34, 0, 2],
      [36, 0, 2], [38, 0, 4], [42, offset, 4]]
      .forEach(([at, value, size]) => (size === 4 ? entry.setUint32(at, value, true) : entry.setUint16(at, value, true)));
    parts.push(local, nameBytes, data);
    central.push(entry, nameBytes);
    offset += 30 + nameBytes.length + data.length;
  }
  const size = central.reduce((sum, part) => sum + part.byteLength, 0);
  const end = new DataView(new ArrayBuffer(22));
  [[0, 0x06054b50, 4], [4, 0, 2], [6, 0, 2], [8, files.length, 2], [10, files.length, 2], [12, size, 4], [16, offset, 4], [20, 0, 2]]
    .forEach(([at, value, sz]) => (sz === 4 ? end.setUint32(at, value, true) : end.setUint16(at, value, true)));
  return new Blob([...parts, ...central, end], { type: 'application/zip' });
}

function downloadAll(month, workbooks) {
  saveBlob(makeZip(workbooks), `Engineer timewriting - ${monthInfo(month).label}.zip`);
}

// Several months at once (the ticked ones, oldest first): one month's journal comes as its
// CSV; more come as one .zip, as do the engineer workbooks.
const monthsLabel = (chosen) => (chosen.length === 1 ? monthInfo(chosen[0].month).label
  : `${monthInfo(chosen[0].month).label} to ${monthInfo(chosen[chosen.length - 1].month).label}`);

function downloadJournals(chosen) {
  const csvs = chosen.map((m) => m.csv).filter(Boolean);
  if (csvs.length === 1) download(csvs[0]);
  else saveBlob(makeZip(csvs), `Timewriting journals - ${monthsLabel(chosen)}.zip`);
}

function downloadWorkbooks(chosen) {
  saveBlob(makeZip(chosen.flatMap((m) => m.workbooks.map(m.pick))), `Engineer timewriting - ${monthsLabel(chosen)}.zip`);
}

const TYPES = { csv: 'text/csv', xlsx: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', md: 'text/markdown' };
function download(file) {
  saveBlob(new Blob([file.data], { type: TYPES[file.name.split('.').pop()] || 'application/octet-stream' }), file.name);
}

async function showJournal(row, month, files) {
  const open = row.nextElementSibling?.classList.contains('journal-row');
  document.querySelectorAll('tr.journal-row').forEach((tr) => tr.remove());
  if (open) return;
  const cell = el('td', { colspan: 3 }, el('p', { class: 'muted' }, 'Loading the journal…'));
  row.after(el('tr', { class: 'journal-row' }, cell));
  try {
    const csv = files.find((f) => f.name.endsWith('.csv'));
    const text = (file) => new TextDecoder().decode(file.data);
    const [header, ...lines] = parseCsv(text(csv)).filter((r) => r.length > 1);
    const readme = files.find((f) => f.name === 'README.md');
    const checksText = readme ? text(readme).split('## Checks')[1] || '' : '';
    const checks = checksText.split('\n').filter((l) => l.startsWith('- ')).map((l) => l.slice(2));
    cell.replaceChildren(
      header && lines.length
        ? journalTable(lines.map((cells) => ({
          cells: cells.map((value, i) => ([2, 3, 4].includes(i) ? money(value) : value)),
          balancing: cells[5] !== '' && cells[6] === '',
        })))
        : el('p', { class: 'muted' }, 'This month’s journal has no lines yet.'),
      el('div', { class: `checks ${checks.length ? 'checks-warn' : 'checks-ok'}` },
        el('strong', {}, checks.length ? 'Check before uploading to Business Central:' : 'No problems found.'),
        checks.length ? el('ul', {}, checks.map((c) => el('li', {}, c))) : null));
  } catch (error) {
    cell.replaceChildren(el('p', { class: 'form-message' }, error.message));
  }
}

// "Include jobs with no hours in the engineer workbooks": remembered on this computer.
const HOURS_ONLY = ' (only jobs with hours).xlsx';
const EMPTY_JOBS_KEY = 'etjm.admin.emptyJobs.v1';
const includeEmptyJobs = () => $('#include-empty')?.checked !== false;

function setUpEmptyJobsOption() {
  const box = $('#include-empty');
  try {
    box.checked = localStorage.getItem(EMPTY_JOBS_KEY) !== 'no';
  } catch { /* storage unavailable: keep the default */ }
  box.addEventListener('change', () => {
    try {
      localStorage.setItem(EMPTY_JOBS_KEY, box.checked ? 'yes' : 'no');
    } catch { /* not remembered */ }
  });
}

// The "Engineer workbooks" lists: one open at a time, closed by a click elsewhere or Escape.
function setUpWorkbookMenus() {
  const openMenus = () => [...document.querySelectorAll('details.workbooks[open]')];
  document.addEventListener('toggle', (e) => {
    if (!e.target.matches?.('details.workbooks') || !e.target.open) return;
    openMenus().forEach((menu) => { if (menu !== e.target) menu.open = false; });
  }, true);
  document.addEventListener('pointerdown', (e) => {
    openMenus().forEach((menu) => { if (!menu.contains(e.target)) menu.open = false; });
  });
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    openMenus().forEach((menu) => { menu.open = false; menu.querySelector('summary').focus(); });
  });
}

async function loadJournals() {
  const box = $('#journals');
  try {
    // One encrypted file per month, with a meaningless name: open them all to see which is which.
    const entries = (await state.repo.list('output')).filter((entry) => entry.type === 'file' && entry.name.endsWith('.json'));
    const opened = await Promise.all(entries.map(async (entry) => JSON.parse(new TextDecoder().decode(await state.repo.read(entry.path)))));
    const byMonth = new Map(opened.filter((m) => /^\d{4}-\d{2}$/.test(m?.month || ''))
      .map((m) => [m.month, Object.entries(m.files || {}).map(([name, b64]) => ({ name, data: b64ToBytes(b64) }))]));
    const months = [...byMonth.keys()].sort().reverse();
    if (!months.length) {
      box.replaceChildren(el('p', { class: 'muted' }, 'No journals yet. They appear here once engineers save their timesheets.'));
      return;
    }
    // What each month offers, newest first.
    const list = months.map((month) => {
      const files = byMonth.get(month);
      // Each engineer's workbook, and (if they have jobs with no hours) a copy without those.
      const workbooks = files.filter((f) => f.name.startsWith('Engineer timewriting') && !f.name.endsWith(HOURS_ONLY));
      const pick = (f) => {
        const short = files.find((g) => g.name === f.name.replace(/\.xlsx$/, HOURS_ONLY));
        return includeEmptyJobs() || !short ? f : { name: f.name, data: short.data };
      };
      return { month, files, csv: files.find((f) => f.name.endsWith('.csv')), workbooks, pick,
        closed: (state.settings?.closedMonths || []).includes(month) };
    });

    // Ticking months swaps each month's download buttons for "Download all" buttons under the list.
    const selected = new Set();
    const ticks = new Map();
    const table = el('table', { class: 'sheets journals' });
    const bulk = el('div', { class: 'bulk-bar', role: 'region', 'aria-label': 'Download the ticked months', hidden: true });
    const allTick = el('input', { type: 'checkbox', class: 'pick', 'aria-label': 'Tick every month' });
    const refresh = () => {
      const chosen = list.filter((m) => selected.has(m.month)).reverse(); // oldest first
      table.classList.toggle('selecting', chosen.length > 0);
      if (chosen.length) table.querySelectorAll('details.workbooks[open]').forEach((menu) => { menu.open = false; });
      for (const [month, tick] of ticks) {
        tick.checked = selected.has(month);
        tick.closest('tr')?.classList.toggle('picked', tick.checked);
      }
      allTick.checked = chosen.length === list.length;
      allTick.indeterminate = chosen.length > 0 && chosen.length < list.length;
      bulk.hidden = !chosen.length;
      if (!chosen.length) return;
      const csvs = chosen.filter((m) => m.csv).length;
      const workbooks = chosen.reduce((sum, m) => sum + m.workbooks.length, 0);
      bulk.replaceChildren(
        el('span', { class: 'bulk-count' }, `${chosen.length} month${chosen.length === 1 ? '' : 's'} ticked`),
        el('button', { class: 'btn btn-primary', type: 'button', disabled: !csvs, onclick: () => downloadJournals(chosen) },
          'Download all journal CSVs'),
        el('button', { class: 'btn', type: 'button', disabled: !workbooks, onclick: () => downloadWorkbooks(chosen) },
          'Download all engineer workbooks'),
        el('button', { class: 'link-btn', type: 'button', onclick: () => { selected.clear(); refresh(); } }, 'Clear'));
    };
    allTick.addEventListener('change', () => {
      if (allTick.checked) list.forEach((m) => selected.add(m.month));
      else selected.clear();
      refresh();
    });

    const rows = list.map(({ month, files, csv, workbooks, pick, closed }) => {
      const tick = el('input', { type: 'checkbox', class: 'pick', 'aria-label': `Tick ${monthInfo(month).label}`, onchange: (e) => {
        if (e.currentTarget.checked) selected.add(month); else selected.delete(month);
        refresh();
      } });
      ticks.set(month, tick);
      return el('tr', {},
        el('th', { scope: 'row' }, el('label', { class: 'month-pick' }, tick,
          el('span', {}, monthInfo(month).label,
            closed ? el('span', { class: 'chip chip-muted chip-inline' }, 'Closed') : null,
            el('span', { class: 'row-note' }, `${workbooks.length} engineer${workbooks.length === 1 ? '' : 's'}`)))),
        el('td', { class: 'download-cell' },
          csv && el('button', { class: 'btn btn-sm btn-primary', type: 'button', onclick: () => download(csv) }, 'Journal CSV'),
          workbooks.length ? el('details', { class: 'workbooks', name: 'workbooks' }, el('summary', { class: 'btn btn-sm' }, 'Engineer workbooks'),
            el('div', { class: 'workbook-list' },
              workbooks.length > 1 ? el('button', { class: 'link-btn link-all', type: 'button', onclick: () => downloadAll(month, workbooks.map(pick)) }, `All (${workbooks.length}, as one .zip)`) : null,
              workbooks.map((f) => el('button', { class: 'link-btn', type: 'button', onclick: () => download(pick(f)) },
              f.name.replace(/^Engineer timewriting - /, '').replace(/ - [A-Za-z]+ \d{4}\.xlsx$/, ''))))) : null),
        el('td', { class: 'row-actions' },
          el('button', { class: 'btn btn-sm', type: 'button', onclick: (e) => showJournal(e.currentTarget.closest('tr'), month, files) }, 'View'),
          el('button', { class: 'btn btn-sm', type: 'button', title: closed ? 'Let engineers change this month again' : 'Freeze this month once its journal is uploaded', onclick: (e) => setClosed(month, !closed, e.currentTarget) }, closed ? 'Reopen' : 'Close')));
    });
    table.replaceChildren(
      el('thead', {}, el('tr', {},
        el('th', { scope: 'col' }, el('label', { class: 'month-pick' }, allTick, el('span', {}, 'Month'))),
        el('th', { scope: 'col' }, 'Download'), el('th', { scope: 'col' }, el('span', { class: 'sr-only' }, 'Actions')))),
      el('tbody', {}, rows));
    box.replaceChildren(table, bulk);
    refresh();
  } catch (error) {
    box.replaceChildren(el('p', { class: 'form-message' }, error.message));
  }
}

// -------------------------------------------------------- engineers & rates

// Save settings.json (and users.json for new passwords), then wait for the website to update.
// With wait: false it returns as soon as the change is saved, with the commit to watch.
async function saveSettings(settings, { resetPasswords = [], message, status, wait = true }) {
  state.busy = true;
  try {
    let commit = null;
    if (resetPasswords.length) {
      const users = await state.repo.readJson(USERS);
      if (users) {
        for (const username of resetPasswords) if (users.data.users?.[username]) users.data.users[username].reset = true;
        ({ commit } = await state.repo.writeJson(USERS, users.data, 'Ask for new passwords (admin page)', users.sha));
      }
    }
    if (JSON.stringify(settings) !== JSON.stringify(state.settings)) {
      const saved = await state.repo.writeJson(SETTINGS, settings, `${message} (admin page)`, state.settingsSha);
      state.settings = settings;
      state.settingsSha = saved.sha;
      commit = saved.commit;
    }
    if (!commit || !wait) return commit;
    status('Saved. Updating the website and journals, about a minute…', 'busy');
    const run = await waitForPublish(commit);
    if (!run) throw new Error('This is taking longer than usual. Reload the page in a few minutes to check.');
    if (run.conclusion !== 'success') throw new Error('The change was saved, but updating the website failed. Look at the “Update website and journals” run in the data repo’s Actions tab.');
    await loadSettings();
    return true;
  } catch (error) {
    if (error.status === 409) {
      await loadSettings();
      throw new Error('Someone else changed the settings at the same time. Their changes are shown now; make yours again.');
    }
    throw error;
  } finally {
    state.busy = false;
  }
}

async function loadSettings() {
  const [settings, users] = await Promise.all([state.repo.readJson(SETTINGS), state.repo.readJson(USERS)]);
  if (!settings) throw new Error('settings.json is missing from the private repo.');
  state.settings = settings.data;
  state.settingsSha = settings.sha;
  state.users = users?.data?.users || {};
  renderStaff();
}

// The same rules as clean_email() in tools/etjm_core.py.
const EMAIL_RE = /^[^@\s<>"',;:()[\]]+@[^@\s<>"',;:()[\]]+\.[^@\s<>"',;:()[\]]+$/;
const cleanEmail = (value) => String(value || '').trim().toLowerCase();

function renderStaff(message) {
  const box = $('#staff');
  const employees = (state.settings.employees || []).map((e) => ({ ...e, username: e.username || defaultUsername(e.name) }));
  const current = employees.filter((e) => e.active !== false);
  const former = employees.filter((e) => e.active === false);
  const admins = (state.settings.admins || []).map((a) => ({ ...a, username: a.username || defaultUsername(a.name) }));
  const edits = { rates: new Map(), emails: new Map(), adminEmails: new Map(), reset: new Set(), remove: new Set(), restore: new Set(), added: [] };
  const status = el('div', { class: 'staff-status', role: 'status' });
  const saveButton = el('button', { class: 'btn btn-primary', type: 'button', disabled: true }, 'Save changes');
  const setStatus = (text, tone = 'muted') => {
    status.dataset.tone = tone;
    status.replaceChildren(...[tone === 'busy' && el('span', { class: 'step-icon', 'aria-hidden': 'true' }), el('span', {}, text)].filter(Boolean));
  };
  const changed = () => {
    saveButton.disabled = state.busy || !(edits.rates.size || edits.emails.size || edits.adminEmails.size || edits.reset.size
      || edits.remove.size || edits.restore.size || edits.added.length);
  };

  const resetButton = (person) => el('button', { class: 'link-btn', type: 'button',
    title: 'Give them a new password when you save. This also signs them out everywhere.', onclick: (e) => {
      const on = !edits.reset.has(person.username);
      if (on) edits.reset.add(person.username); else edits.reset.delete(person.username);
      e.currentTarget.textContent = on ? 'Keep password' : 'New password';
      e.currentTarget.closest('tr').classList.toggle('pending', on || edits.remove.has(person.username));
      changed();
    } }, 'New password');

  const passwordCell = (person) => {
    const record = state.users[person.username];
    if (!record) return el('td', { class: 'muted' }, 'Made when saved');
    if (record.own || !record.password) {
      return el('td', { class: 'password-cell' }, el('span', { class: 'muted' }, 'Chosen by them'), resetButton(person));
    }
    const reveal = () => issuedPassword(state.profile, person.username, record.password);
    const shown = el('code', { class: 'password' }, '••••-••••-••••');
    return el('td', { class: 'password-cell' }, shown,
      el('button', { class: 'link-btn', type: 'button', onclick: async (e) => {
        const button = e.currentTarget;
        const show = shown.textContent.startsWith('•');
        shown.textContent = show ? await reveal() : '••••-••••-••••';
        button.textContent = show ? 'Hide' : 'Show';
      } }, 'Show'),
      el('button', { class: 'link-btn', type: 'button', onclick: async () => {
        try {
          await navigator.clipboard.writeText(await reveal());
          toast(`Copied ${person.name}’s password.`);
        } catch {
          toast('Couldn’t copy. Use Show and copy it by hand.', { tone: 'error' });
        }
      } }, 'Copy'),
      resetButton(person));
  };

  // Where this person's password-reset emails go (see "Forgot your password?").
  const emailInput = (person, map) => el('input', {
    class: 'email-input', type: 'email', inputmode: 'email', autocomplete: 'off', spellcheck: false, maxLength: 254,
    value: person.email || '', placeholder: 'name@company.co.nz', 'aria-label': `Email address for ${person.name}`,
    oninput: (e) => {
      const value = cleanEmail(e.currentTarget.value);
      e.currentTarget.classList.toggle('invalid', Boolean(value) && !EMAIL_RE.test(value));
      if (value === (person.email || '')) map.delete(person.username);
      else map.set(person.username, value);
      changed();
    },
  });

  // Shown as plain text; "Change" (or "Add email") opens a box for it, and "Cancel" puts it back.
  const emailField = (person, map) => {
    const box = el('div', { class: 'email-field' });
    const edit = () => {
      const input = emailInput(person, map);
      box.replaceChildren(input, el('button', { class: 'link-btn', type: 'button', onclick: show }, 'Cancel'));
      input.focus();
      input.select();
    };
    const show = () => {
      map.delete(person.username);
      box.replaceChildren(
        person.email ? el('span', { class: 'email-text' }, person.email) : el('span', { class: 'muted' }, 'No email yet'),
        el('button', { class: 'link-btn', type: 'button', onclick: edit, 'aria-label': `${person.email ? 'Change' : 'Add'} the email address for ${person.name}` },
          person.email ? 'Change' : 'Add email'));
      changed();
    };
    show();
    return box;
  };

  const rateInput = (person) => el('input', {
    class: 'rate-input', type: 'number', min: '1', step: '0.01', inputmode: 'decimal', required: true,
    value: person.rechargeRate ?? '', 'aria-label': `Recharge rate for ${person.name}`,
    oninput: (e) => {
      const value = e.currentTarget.value;
      if (String(person.rechargeRate ?? '') === value) edits.rates.delete(person.username);
      else edits.rates.set(person.username, value);
      changed();
    },
  });

  const rows = current.map((person) => el('tr', { dataset: { username: person.username } },
    el('th', { scope: 'row' }, person.name),
    el('td', { class: 'muted' }, person.username),
    el('td', {}, emailField(person, edits.emails)),
    el('td', {}, el('span', { class: 'rate-prefix' }, '$'), rateInput(person)),
    passwordCell(person),
    el('td', { class: 'row-actions' },
      el('a', { class: 'btn btn-sm', href: `home.html?as=${encodeURIComponent(person.username)}`, title: `See ${person.name}’s timesheets as they do (read-only)` }, 'View'),
      el('button', { class: 'btn btn-sm', type: 'button', onclick: (e) => {
      const on = !edits.remove.has(person.username);
      if (on) edits.remove.add(person.username); else edits.remove.delete(person.username);
      e.currentTarget.textContent = on ? 'Undo' : 'Remove';
      e.currentTarget.closest('tr').classList.toggle('removing', on);
      changed();
    } }, 'Remove'))));

  const addedBody = el('tbody', { class: 'added' });
  const newName = el('input', { class: 'add-input', placeholder: 'Full name, as in the journal', maxLength: 60, 'aria-label': 'New engineer’s name' });
  const newEmail = el('input', { class: 'email-input', type: 'email', inputmode: 'email', autocomplete: 'off', spellcheck: false, maxLength: 254,
    placeholder: 'Email (for password resets)', 'aria-label': 'New engineer’s email address' });
  const newRate = el('input', { class: 'rate-input', type: 'number', min: '1', step: '0.01', placeholder: '160', 'aria-label': 'New engineer’s recharge rate' });
  const addProblem = el('p', { class: 'form-message', hidden: true });
  const taken = () => new Set([...employees.map((e) => e.username), ...edits.added.map((e) => e.username)]);
  const add = () => {
    const name = cleanText(newName.value);
    const rate = Number(newRate.value);
    const email = cleanEmail(newEmail.value);
    const problem = nameProblem(name)
      || (employees.concat(edits.added).some((e) => nameKey(e.name) === nameKey(name)) ? `${name} is already on the list.` : null)
      || (email && !EMAIL_RE.test(email) ? 'That email address doesn’t look right.' : null)
      || (!(rate > 0) ? 'Enter their recharge rate, e.g. 160.' : null)
      || (!defaultUsername(name) || taken().has(defaultUsername(name)) ? 'Someone already has that username. Add them in settings.json with a different “username”.' : null);
    addProblem.hidden = !problem;
    addProblem.textContent = problem || '';
    if (problem) return;
    const person = { name, username: defaultUsername(name), rechargeRate: rate, ...(email ? { email } : {}) };
    edits.added.push(person);
    addedBody.append(el('tr', { class: 'pending' },
      el('th', { scope: 'row' }, name, el('span', { class: 'chip chip-ok chip-inline' }, 'New')),
      el('td', { class: 'muted' }, person.username), el('td', { class: 'muted' }, person.email || '—'), el('td', {}, `$${rate}`),
      el('td', { class: 'muted' }, 'Made when saved'),
      el('td', { class: 'row-actions' }, el('button', { class: 'btn btn-sm', type: 'button', onclick: (e) => {
        edits.added.splice(edits.added.indexOf(person), 1);
        e.currentTarget.closest('tr').remove();
        changed();
      } }, 'Undo'))));
    newName.value = '';
    newEmail.value = '';
    newRate.value = '';
    newName.focus();
    changed();
  };
  newName.addEventListener('keydown', (e) => { if (e.key === 'Enter') add(); });
  newRate.addEventListener('keydown', (e) => { if (e.key === 'Enter') add(); });
  newEmail.addEventListener('keydown', (e) => { if (e.key === 'Enter') add(); });

  // The people who can use this page. Their own reset emails are set here.
  const officeTable = admins.length ? el('div', { class: 'office' },
    el('h3', {}, 'Office'),
    el('p', { class: 'muted' }, 'People who can use this Admin page. Add an email address so they can reset a forgotten password.'),
    el('table', { class: 'sheets staff office-table' },
      el('thead', {}, el('tr', {}, el('th', { scope: 'col' }, 'Name'), el('th', { scope: 'col' }, 'Username'), el('th', { scope: 'col' }, 'Email'))),
      el('tbody', {}, admins.map((person) => el('tr', {},
        el('th', { scope: 'row' }, person.name), el('td', { class: 'muted' }, person.username),
        el('td', {}, emailField(person, edits.adminEmails))))))) : null;

  const formerList = former.length ? el('details', { class: 'former' },
    el('summary', {}, `Former engineers (${former.length})`),
    el('p', { class: 'muted' }, 'They can’t sign in. Their past timesheets stay in the journals at their old rate.'),
    el('ul', {}, former.map((person) => el('li', {}, `${person.name} ($${person.rechargeRate ?? '?'})`,
      el('a', { class: 'link-btn', href: `home.html?as=${encodeURIComponent(person.username)}` }, 'View'),
      el('button', { class: 'link-btn', type: 'button', onclick: (e) => {
        const on = !edits.restore.has(person.username);
        if (on) edits.restore.add(person.username); else edits.restore.delete(person.username);
        e.currentTarget.textContent = on ? 'Undo' : 'Restore';
        changed();
      } }, 'Restore'))))) : null;

  saveButton.addEventListener('click', async () => {
    const bad = [...edits.rates].find(([, value]) => !(Number(value) > 0));
    if (bad) {
      setStatus('Every engineer needs a recharge rate above 0.', 'error');
      return;
    }
    const badEmail = [...edits.emails, ...edits.adminEmails].find(([, value]) => value && !EMAIL_RE.test(value));
    if (badEmail) {
      setStatus(`The email address for ${badEmail[0]} doesn’t look right.`, 'error');
      return;
    }
    const withEmail = (person, map, username) => {
      if (!map.has(username)) return person;
      const next = { ...person };
      if (map.get(username)) next.email = map.get(username); else delete next.email;
      return next;
    };
    const settings = structuredClone(state.settings);
    settings.employees = (settings.employees || []).map((e) => {
      const username = e.username || defaultUsername(e.name);
      const next = { ...e, username };
      if (edits.rates.has(username)) next.rechargeRate = Number(edits.rates.get(username));
      if (edits.remove.has(username)) next.active = false;
      if (edits.restore.has(username)) delete next.active;
      return withEmail(next, edits.emails, username);
    }).concat(edits.added);
    settings.admins = (settings.admins || []).map((a) => withEmail(a, edits.adminEmails, a.username || defaultUsername(a.name)));
    delete settings.defaultRechargeRate;
    saveButton.disabled = true;
    setStatus('Saving…', 'busy');
    try {
      const newPeople = edits.added.map((e) => e.username).concat([...edits.restore], [...edits.reset]);
      await saveSettings(settings, { resetPasswords: [...edits.reset], message: 'Change engineers', status: setStatus });
      const ready = newPeople.filter((u) => state.users[u]?.password);
      renderStaff(ready.length ? `Done. Passwords for ${ready.join(', ')} are in the list: use Show or Copy to hand them out.` : 'Done. The website and journals are up to date.');
      loadJournals();
    } catch (error) {
      setStatus(error.message, 'error');
      changed();
    }
  });

  // (replaceChildren would show a missing part as the word "null", so leave those out.)
  box.replaceChildren(...[
    el('table', { class: 'sheets staff' },
      el('thead', {}, el('tr', {},
        el('th', { scope: 'col' }, 'Name'), el('th', { scope: 'col' }, 'Username'), el('th', { scope: 'col' }, 'Email'),
        el('th', { scope: 'col' }, 'Rate / hour'),
        el('th', { scope: 'col' }, 'Password'), el('th', { scope: 'col' }, el('span', { class: 'sr-only' }, 'Actions')))),
      el('tbody', {}, rows), addedBody),
    el('div', { class: 'add-row' }, newName, newEmail, el('span', { class: 'rate-prefix' }, '$'), newRate,
      el('button', { class: 'btn', type: 'button', onclick: add }, 'Add engineer')),
    addProblem,
    formerList,
    officeTable,
    el('div', { class: 'staff-actions' }, saveButton, status),
  ].filter(Boolean));
  if (message) setStatus(message, 'ok');
  changed();
}

async function setClosed(month, close, button) {
  if (state.busy) {
    toast('Another change is still being saved. Try again in a minute.');
    return;
  }
  const label = monthInfo(month).label;
  const settings = structuredClone(state.settings);
  const months = new Set(settings.closedMonths || []);
  if (close) months.add(month); else months.delete(month);
  settings.closedMonths = [...months].sort();
  // Saving takes a second or two; the website and journals then update in the background
  // (about half a minute), so the button doesn't wait for that.
  const was = button?.textContent;
  if (button) {
    button.disabled = true;
    button.textContent = close ? 'Closing…' : 'Reopening…';
  }
  let commit;
  try {
    commit = await saveSettings(settings, { message: `${close ? 'Close' : 'Reopen'} a month`, status: () => {}, wait: false });
  } catch (error) {
    notice('error', error.message);
    if (button) {
      button.disabled = false;
      button.textContent = was;
    }
    return;
  }
  if (close) fireworks();
  toast(close
    ? `${label} is closed: its journal is frozen and engineers can no longer change it.`
    : `${label} is open again: engineers can change it, and its journal is rebuilt in about half a minute.`, { tone: 'ok', duration: 7000 });
  await loadJournals();
  if (!commit) return;
  const run = await waitForPublish(commit).catch(() => null);
  if (run && run.conclusion !== 'success') {
    notice('error', `${label} was ${close ? 'closed' : 'reopened'}, but updating the website failed. Look at the “Update website and journals” run in the data repo’s Actions tab.`);
  } else if (run && !close) {
    loadJournals(); // show the rebuilt journal
  }
}

// ------------------------------------------------------------------- start

async function start() {
  try {
    const opened = await openSession(session);
    state.bundle = opened.bundle;
    state.profile = opened.profile;
  } catch (error) {
    if (error.code === 'signin') {
      clearSession();
      location.replace(`index.html?why=${error.reason || 'again'}&next=admin.html`);
      return;
    }
    notice('error', error.message);
    return;
  }
  $('#user-name').textContent = state.profile.name;
  $('#timesheets-link').hidden = !state.profile.timesheets;
  if (!state.profile.admin) {
    $('#jobs-card').hidden = true;
    $('#staff-card').hidden = true;
    $('#journals-card').hidden = true;
    notice('info', 'This page is for the office. Use My timesheets to fill in your hours.');
    return;
  }
  const updated = state.bundle.jobsUpdated
    ? new Date(`${state.bundle.jobsUpdated}T12:00:00`).toLocaleDateString('en-NZ', { day: 'numeric', month: 'short', year: 'numeric' })
    : '';
  $('#jobs-current').textContent = `The website lists ${state.bundle.jobs.length.toLocaleString('en-NZ')} open jobs from “${state.bundle.jobsSource}”${updated ? `, updated ${updated}` : ''}.`;
  if (!state.profile.adminToken) {
    notice('warn', 'Uploading and journals aren’t switched on yet: the admin key (ADMIN_TOKEN) still needs adding to the data repo.');
    $('#dropzone').classList.add('disabled');
    $('#report-file').disabled = true;
    $('#journals').replaceChildren(el('p', { class: 'muted' }, 'Available once the admin key is set up.'));
    $('#staff').replaceChildren(el('p', { class: 'muted' }, 'Available once the admin key is set up.'));
    return;
  }
  state.repo = adminRepo(state.bundle, state.profile);
  setUpUpload();
  setUpEmptyJobsOption();
  setUpWorkbookMenus();
  try {
    await loadSettings();
  } catch (error) {
    $('#staff').replaceChildren(el('p', { class: 'form-message' }, error.message));
  }
  loadJournals();
}

if (session?.uk) start();
