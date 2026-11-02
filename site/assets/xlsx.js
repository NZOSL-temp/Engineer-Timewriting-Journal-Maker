// Reading an engineer's own timesheet spreadsheet (.xlsx) in the browser: the first sheet's
// cells, then the month in it. The file never leaves the computer.
//
// Every layout in use (the website's own engineer workbook, and the sheets engineers keep
// themselves) has a header row with "Job Number" and one column per date, other time on
// rows marked N/A, and job rows with a job number (JOB009683, J9683-CE-80 or 9683), sometimes
// typed at the start of the description instead. CAPEX/REVEX comes from the Capex/Revex column,
// or from a "Manually selected" column where someone filled that in.

const decoder = new TextDecoder();

// ------------------------------------------------------------------ the zip

function entries(bytes) {
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  let end = -1;
  for (let i = bytes.length - 22; i >= Math.max(0, bytes.length - 65557); i -= 1) {
    if (view.getUint32(i, true) === 0x06054b50) {
      end = i;
      break;
    }
  }
  if (end < 0) throw new Error('This isn’t an Excel (.xlsx) file.');
  const files = new Map();
  let at = view.getUint32(end + 16, true);
  for (let n = view.getUint16(end + 10, true); n > 0; n -= 1) {
    if (view.getUint32(at, true) !== 0x02014b50) throw new Error('This Excel file looks damaged.');
    const nameLength = view.getUint16(at + 28, true);
    files.set(decoder.decode(bytes.subarray(at + 46, at + 46 + nameLength)), {
      method: view.getUint16(at + 10, true), size: view.getUint32(at + 20, true), local: view.getUint32(at + 42, true),
    });
    at += 46 + nameLength + view.getUint16(at + 30, true) + view.getUint16(at + 32, true);
  }
  const read = async (name) => {
    const file = files.get(name);
    if (!file) return null;
    const start = file.local + 30 + view.getUint16(file.local + 26, true) + view.getUint16(file.local + 28, true);
    const data = bytes.subarray(start, start + file.size);
    if (file.method === 0) return decoder.decode(data);
    if (file.method !== 8) throw new Error('This Excel file is packed in a way the website can’t read. Save it again from Excel.');
    return new Response(new Blob([data]).stream().pipeThrough(new DecompressionStream('deflate-raw'))).text();
  };
  return read;
}

// ---------------------------------------------------------------- the sheet

const unescapeXml = (text) => text.replace(/&(amp|lt|gt|quot|apos|#x?[0-9a-fA-F]+);/g, (m, e) => (
  { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'" }[e]
  ?? String.fromCodePoint(e[1] === 'x' ? parseInt(e.slice(2), 16) : Number(e.slice(1)))));
const textOf = (xml) => unescapeXml([...xml.matchAll(/<t(?:\s[^>]*)?>([^<]*)<\/t>/g)].map((m) => m[1]).join(''));
const attr = (tag, name) => (tag.match(new RegExp(`\\s${name}="([^"]*)"`)) || [])[1];

function column(ref) {
  let n = 0;
  for (const ch of ref.replace(/\d+$/, '')) n = n * 26 + ch.charCodeAt(0) - 64;
  return n - 1;
}

// The first sheet (the left-most tab) as rows of cells: numbers, text or null.
export async function readFirstSheet(bytes) {
  if (bytes[0] !== 0x50 || bytes[1] !== 0x4b) throw new Error('This isn’t an Excel (.xlsx) file. Save it from Excel as .xlsx first.');
  if (typeof DecompressionStream !== 'function') throw new Error('This browser can’t open Excel files. Try a newer Chrome, Edge, Safari or Firefox.');
  const read = entries(bytes);
  const workbook = await read('xl/workbook.xml');
  const first = workbook && (workbook.match(/<sheet\s[^>]*\/?>/) || [])[0];
  if (!first) throw new Error('This Excel file has no sheets.');
  const id = attr(first, 'r:id');
  const rels = (await read('xl/_rels/workbook.xml.rels')) || '';
  const rel = [...rels.matchAll(/<Relationship\s[^>]*\/?>/g)].map((m) => m[0]).find((r) => attr(r, 'Id') === id);
  let target = rel ? attr(rel, 'Target') : 'worksheets/sheet1.xml';
  target = target.startsWith('/') ? target.slice(1) : `xl/${target}`;
  const xml = await read(target);
  if (!xml) throw new Error('The first sheet of this Excel file couldn’t be read.');
  const shared = [...((await read('xl/sharedStrings.xml')) || '').matchAll(/<si>([\s\S]*?)<\/si>/g)].map((m) => textOf(m[1]));

  const rows = [];
  for (const [, rowTag, body] of xml.matchAll(/<row(\s[^>]*)?>([\s\S]*?)<\/row>/g)) {
    const r = Number(attr(rowTag || '', 'r')) - 1;
    const cells = [];
    for (const cell of body.matchAll(/<c(\s[^>]*?)(?:\/>|>([\s\S]*?)<\/c>)/g)) {
      const ref = attr(cell[1], 'r');
      const type = attr(cell[1], 't');
      const inner = cell[2] || '';
      const v = (inner.match(/<v>([^<]*)<\/v>/) || [])[1];
      let value = null;
      if (type === 's') value = shared[Number(v)] ?? null;
      else if (type === 'inlineStr') value = textOf(inner);
      else if (type === 'str' || type === 'e') value = v == null ? null : unescapeXml(v);
      else if (type === 'b') value = v === '1';
      else if (v != null && v !== '') value = Number(v);
      if (ref) cells[column(ref)] = value;
    }
    rows[Number.isFinite(r) && r >= 0 ? r : rows.length] = cells;
  }
  return { name: unescapeXml(attr(first, 'name') || 'Sheet1'), rows: Array.from(rows, (row) => row || []) };
}

// -------------------------------------------------------- the month in it

const EXCEL_EPOCH = Date.UTC(1899, 11, 30);
const asDate = (value) => (typeof value === 'number' && value > 20000 && value < 80000
  ? new Date(EXCEL_EPOCH + Math.round(value) * 86400000) : null);
const text = (value) => (value == null ? '' : String(value).replace(/\s+/g, ' ').trim());
const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];

// "JOB009683", "J9683-CE-80", "JOB 9683" or "9683" -> "JOB009683" (null if it isn't a job number).
export function jobNumber(value) {
  const t = text(value).toUpperCase();
  const match = t.match(/^J(?:OB)?\s*0*(\d{1,6})(?:\b|[-\s])/) || t.match(/^0*(\d{3,6})$/);
  return match ? `JOB${match[1].padStart(6, '0')}` : null;
}

function hoursFrom(row, dayColumns) {
  const hours = {};
  for (const [col, day] of dayColumns) {
    const raw = row[col];
    const value = typeof raw === 'number' ? raw : Number(text(raw));
    if (Number.isFinite(value) && value > 0) hours[day] = (hours[day] || 0) + Math.round(value * 100) / 100;
  }
  return hours;
}

const add = (into, hours) => {
  for (const [day, value] of Object.entries(hours)) into[day] = Math.round(((into[day] || 0) + value) * 100) / 100;
};

/**
 * The month `month` ("2026-10") from a sheet read by readFirstSheet, in the saved-timesheet
 * shape ({ jobs, other }). `jobs` maps job numbers to the job list's entries (for CAPEX/REVEX
 * when the sheet doesn't say), `otherTime` lists the office's other-time names. Returns
 * { data, notes, name }: notes are things to tell the engineer; name is the sheet's NAME.
 * Throws if the sheet isn't a timesheet, or is for another month.
 */
export function timesheetFromSheet(sheet, month, { jobs = new Map(), otherTime = [] } = {}) {
  const [year, number] = month.split('-').map(Number);
  const days = new Date(Date.UTC(year, number, 0)).getUTCDate();
  const rows = sheet.rows;
  const headerAt = rows.findIndex((row) => row.some((cell) => /^job\s*(number|no\.?)$/i.test(text(cell))));
  if (headerAt < 0) {
    throw new Error(`The first sheet (“${sheet.name}”) has no “Job Number” heading, so it doesn’t look like a timesheet.`);
  }
  const header = rows[headerAt];
  const find = (re) => header.findIndex((cell) => re.test(text(cell)));
  const cols = {
    job: find(/^job\s*(number|no\.?)$/i), location: find(/^location$/i), owner: find(/^owner$/i),
    description: find(/^description/i), task: find(/capex|revex/i), manual: find(/manual/i),
  };
  const taskIn = (value) => (/capex/i.test(value) ? 'CAPEX' : /revex/i.test(value) ? 'REVEX' : '');

  // Day columns: dates in the heading row (or the row above it), else day numbers 1-31.
  const dayColumns = new Map();
  const otherMonths = new Set();
  for (const row of [header, rows[headerAt - 1] || []]) {
    row.forEach((cell, col) => {
      const date = asDate(cell);
      if (!date || dayColumns.has(col)) return;
      if (date.getUTCFullYear() === year && date.getUTCMonth() + 1 === number) dayColumns.set(col, date.getUTCDate());
      else otherMonths.add(`${MONTHS[date.getUTCMonth()]} ${date.getUTCFullYear()}`);
    });
    if (dayColumns.size) break;
  }
  if (!dayColumns.size) {
    header.forEach((cell, col) => {
      if (col > Math.max(cols.job, cols.description, cols.task) && Number.isInteger(cell) && cell >= 1 && cell <= days) dayColumns.set(col, cell);
    });
  }
  const label = `${MONTHS[number - 1]} ${year}`;
  if (!dayColumns.size) {
    throw new Error(otherMonths.size
      ? `This spreadsheet is for ${[...otherMonths][0]}, not ${label}. Open ${label} on the website first, or use that month’s spreadsheet.`
      : 'The “Job Number” heading row has no dates (or day numbers) above the hours, so the days can’t be matched up.');
  }

  const notes = [];
  const jobRows = new Map(); // job -> row in the saved shape
  const otherRows = new Map(); // label -> row
  const known = new Map(otherTime.map((name) => [name.toLowerCase(), name]));
  const guessed = [];
  for (const row of rows.slice(headerAt + 1)) {
    const cell = (key) => (cols[key] >= 0 ? text(row[cols[key]]) : '');
    const description = cell('description');
    const jobCell = cell('job');
    if (/^total/i.test(description) || /^total/i.test(jobCell)) break; // anything below the totals is a summary
    if (/daily\s*hours/i.test(description)) continue;
    const hours = hoursFrom(row, dayColumns);
    const job = jobNumber(jobCell) || (jobCell ? null : jobNumber(description));
    if (job) {
      const info = jobs.get(job);
      let task = taskIn(cell('manual')) || taskIn(cell('task'));
      if (!task && Object.keys(hours).length) {
        const open = info?.tasks || [];
        task = open.length === 1 ? open[0] : 'REVEX';
        if (open.length !== 1) guessed.push(job);
      }
      let entry = jobRows.get(job);
      if (!entry) {
        entry = { job, location: info?.loc ?? cell('location'), owner: info?.owner ?? cell('owner'), description: info?.desc ?? description, hours: {} };
        jobRows.set(job, entry);
      }
      if (task) add(entry.hours[task] ||= {}, hours);
      continue;
    }
    // Other time: rows marked N/A (or with no job), named in the description.
    if (!description || !Object.keys(hours).length) continue; // headings, empty template rows
    if (jobCell && !/^n\/?a$/i.test(jobCell)) continue; // a section heading like "CENTRAL"
    const name = known.get(description.toLowerCase()) || description;
    add((otherRows.get(name) || otherRows.set(name, { label: name, hours: {} }).get(name)).hours, hours);
  }
  if (guessed.length) {
    notes.push(`The spreadsheet doesn’t say CAPEX or REVEX for ${guessed.length === 1 ? 'job' : 'jobs'} ${guessed.join(', ')}, so their hours went on REVEX. Move them if they should be CAPEX.`);
  }
  const closed = [...jobRows.keys()].filter((job) => !jobs.has(job));
  if (closed.length) notes.push(`${closed.length === 1 ? 'Job' : 'Jobs'} ${closed.join(', ')} ${closed.length === 1 ? 'isn’t' : 'aren’t'} open in Business Central.`);
  const nameRow = rows.slice(0, headerAt).find((row) => /^name$/i.test(text(row[0])));
  return { data: { jobs: [...jobRows.values()], other: [...otherRows.values()] }, notes, name: nameRow ? text(nameRow[1]) : '' };
}
