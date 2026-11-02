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
    if (at + 46 > bytes.length || view.getUint32(at, true) !== 0x02014b50) throw new Error('This Excel file looks damaged. Save it again from Excel.');
    const nameLength = view.getUint16(at + 28, true);
    const name = decoder.decode(bytes.subarray(at + 46, at + 46 + nameLength)).replace(/\\/g, '/').replace(/^\//, '');
    files.set(name.toLowerCase(), {
      method: view.getUint16(at + 10, true), size: view.getUint32(at + 20, true),
      unpacked: view.getUint32(at + 24, true), local: view.getUint32(at + 42, true),
    });
    at += 46 + nameLength + view.getUint16(at + 30, true) + view.getUint16(at + 32, true);
  }
  // One file's text, or null if there's no such file (names aren't case-sensitive in a zip from Excel).
  return async (path) => {
    const file = files.get(path.toLowerCase());
    if (!file) return null;
    if (file.unpacked > 200 * 1024 * 1024) throw new Error('This Excel file is far too big to be a timesheet.');
    const start = file.local + 30 + view.getUint16(file.local + 26, true) + view.getUint16(file.local + 28, true);
    const data = bytes.subarray(start, start + file.size);
    if (file.method === 0) return decoder.decode(data);
    if (file.method !== 8) throw new Error('This Excel file is packed in a way the website can’t read. Save it again from Excel.');
    return new Response(new Blob([data]).stream().pipeThrough(new DecompressionStream('deflate-raw'))).text();
  };
}

// ---------------------------------------------------------------- the sheet

const unescapeXml = (text) => text.replace(/&(amp|lt|gt|quot|apos|#x[0-9a-fA-F]+|#\d+);/g, (m, e) => (
  { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'" }[e]
  ?? String.fromCodePoint(e[1] === 'x' ? parseInt(e.slice(2), 16) : Number(e.slice(1)))));
// Element names without a namespace prefix (some programs write <x:row> for <row>).
const plain = (xml) => (xml || '').replace(/<(\/?)[A-Za-z_][\w.-]*:/g, '<$1');
// The text of <t> elements, leaving out phonetic guides (<rPh>).
const textOf = (xml) => unescapeXml([...xml.replace(/<rPh\b[\s\S]*?<\/rPh>/g, '')
  .matchAll(/<t(?:\s[^>]*)?>([^<]*)<\/t>/g)].map((m) => m[1]).join(''));
// An attribute's value, in double or single quotes, with or without a prefix (r:id, id).
const attr = (tag, name) => {
  const m = tag.match(new RegExp(`\\s(?:[A-Za-z_][\\w.-]*:)?${name}\\s*=\\s*(?:"([^"]*)"|'([^']*)')`));
  return m ? unescapeXml(m[1] ?? m[2]) : undefined;
};
const tags = (xml, name) => [...xml.matchAll(new RegExp(`<${name}(?=[\\s/>])[^>]*>`, 'g'))].map((m) => m[0]);

// Cell styles (by index) that show a time of day or a duration, like 7:30 or [h]:mm: such a
// cell holds a fraction of a day (7:30 is 0.3125), not hours.
const isTimeCode = (code) => {
  const bare = code.replace(/"[^"]*"|\\.|\[(?!(?:h+|m+|s+)\])[^\]]*\]/gi, '');
  return /h/i.test(bare) && !/[dy]/i.test(bare);
};
function timeStyles(xml) {
  const custom = new Map(tags(xml, 'numFmt').map((t) => [Number(attr(t, 'numFmtId')), attr(t, 'formatCode') || '']));
  const cellXfs = (xml.match(/<cellXfs\b[\s\S]*?<\/cellXfs>/) || [''])[0];
  const out = new Set();
  tags(cellXfs, 'xf').forEach((tag, index) => {
    const id = Number(attr(tag, 'numFmtId') || 0);
    if ([18, 19, 20, 21, 45, 46, 47].includes(id) || (custom.has(id) && isTimeCode(custom.get(id)))) out.add(index);
  });
  return out;
}

function column(ref) {
  let n = 0;
  for (const ch of ref.toUpperCase().replace(/[^A-Z]/g, '')) n = n * 26 + ch.charCodeAt(0) - 64;
  return n - 1;
}

// "xl/worksheets/sheet1.xml" for a relationship target, from the part that refers to it.
function resolve(from, target) {
  if (target.startsWith('/')) return target.slice(1);
  const parts = from.split('/').slice(0, -1);
  for (const piece of target.split('/')) {
    if (piece === '..') parts.pop();
    else if (piece && piece !== '.') parts.push(piece);
  }
  return parts.join('/');
}

async function relationships(read, part) {
  const dir = part.split('/').slice(0, -1).join('/');
  const file = part.split('/').pop();
  const xml = plain(await read(`${dir ? `${dir}/` : ''}_rels/${file}.rels`));
  return tags(xml, 'Relationship').map((tag) => ({
    id: attr(tag, 'Id'), type: attr(tag, 'Type') || '', target: resolve(part, attr(tag, 'Target') || ''),
  }));
}

// The workbook's parts: how to read files from it, its sheets (in tab order), shared strings
// and which cell styles are times. Throws if it isn't an .xlsx.
async function openWorkbook(bytes) {
  if (bytes[0] !== 0x50 || bytes[1] !== 0x4b) throw new Error('This isn’t an Excel (.xlsx) file. In Excel, use File › Save As › Excel Workbook (.xlsx).');
  if (typeof DecompressionStream !== 'function') throw new Error('This browser can’t open Excel files. Try a newer Chrome, Edge, Safari or Firefox.');
  const read = entries(bytes);
  const main = (await relationships(read, '')).find((r) => /\/officeDocument$/.test(r.type))?.target || 'xl/workbook.xml';
  const workbook = plain(await read(main));
  const rels = await relationships(read, main);
  const sheets = tags(workbook, 'sheet').map((tag, i) => ({
    name: attr(tag, 'name') || `Sheet${i + 1}`,
    path: rels.find((r) => r.id === attr(tag, 'id'))?.target || resolve(main, `worksheets/sheet${i + 1}.xml`),
  }));
  if (!sheets.length) throw new Error('This Excel file has no sheets.');
  const sharedPart = rels.find((r) => /\/sharedStrings$/.test(r.type))?.target || resolve(main, 'sharedStrings.xml');
  return {
    read,
    sheets,
    date1904: /^(1|true)$/i.test(attr(tags(workbook, 'workbookPr')[0] || '', 'date1904') || ''),
    // Set by programs that write formulas without working them out; Excel clears it when it saves.
    recalc: /^(1|true)$/i.test(attr(tags(workbook, 'calcPr')[0] || '', 'fullCalcOnLoad') || ''),
    times: timeStyles(plain(await read(rels.find((r) => /\/styles$/.test(r.type))?.target || resolve(main, 'styles.xml')))),
    shared: [...plain(await read(sharedPart)).matchAll(/<si\b[^>]*?(?:\/>|>([\s\S]*?)<\/si>)/g)].map((m) => textOf(m[1] || '')),
  };
}

// One sheet's rows (the first `maxRows` of them, if given).
function sheetRows(xml, book, maxRows = Infinity) {
  const rows = [];
  let nextRow = 0;
  for (const [, rowTag, , body] of xml.matchAll(/<row\b([^>]*?)(\/>|>([\s\S]*?)<\/row>)/g)) {
    const r = Number(attr(rowTag, 'r'));
    const index = Number.isInteger(r) && r >= 1 ? r - 1 : nextRow;
    if (index >= maxRows) break;
    nextRow = index + 1;
    const cells = [];
    let nextCol = 0;
    for (const [, cellTag, , inner = ''] of (body || '').matchAll(/<c\b([^>]*?)(\/>|>([\s\S]*?)<\/c>)/g)) {
      const ref = attr(cellTag, 'r');
      const col = ref ? column(ref) : nextCol;
      nextCol = col + 1;
      const type = attr(cellTag, 't');
      const v = (inner.match(/<v\b[^>]*>([^<]*)<\/v>/) || [])[1];
      let value = null;
      if (type === 's') value = book.shared[Number(v)] ?? null;
      else if (type === 'inlineStr') value = textOf(inner);
      else if (type === 'str' || type === 'e') value = v == null ? null : unescapeXml(v);
      else if (type === 'b') value = v == null ? null : v === '1' || v === 'true';
      else if (type === 'd') value = v ? { iso: unescapeXml(v) } : null; // a date written as text (rare)
      else if (v != null && v.trim() !== '') value = book.times.has(Number(attr(cellTag, 's'))) ? { days: Number(v) } : Number(v);
      if (v == null && /<f\b/.test(inner)) value = { formula: true }; // a formula never worked out
      cells[col] = value;
    }
    rows[index] = cells;
  }
  return Array.from(rows, (row) => row || []);
}

// The first sheet (the left-most tab) as rows of cells: numbers, text, true/false, null, or
// { days } for a cell shown as a time (7:30), { iso } for a date written as text and
// { formula: true } for a formula with no worked-out value.
// date1904 is true for workbooks using the old Mac date system.
export async function readFirstSheet(bytes) {
  const book = await openWorkbook(bytes);
  const [first] = book.sheets;
  const xml = plain(await book.read(first.path));
  if (!xml) throw new Error('The first sheet of this Excel file couldn’t be read. Is it a chart rather than a sheet?');
  return { name: first.name, date1904: book.date1904, recalc: book.recalc, rows: sheetRows(xml, book) };
}

// The first rows of every sheet, as text: [{ name, rows: [[text, ...], ...] }]. For checking
// a file's column headings without reading all of it.
export async function sheetTops(bytes, maxRows = 25) {
  const book = await openWorkbook(bytes);
  const out = [];
  for (const sheet of book.sheets) {
    const xml = plain(await book.read(sheet.path));
    if (!xml) continue;
    out.push({ name: sheet.name, rows: sheetRows(xml, book, maxRows).map((row) => Array.from(row, (cell) => text(cell))) });
  }
  return out;
}

// -------------------------------------------------------- the month in it

const DAY = 86400000;
const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];
const text = (value) => (value == null || typeof value === 'object' ? '' : String(value).replace(/\s+/g, ' ').trim());

// A cell as a date (UTC midnight), or null: an Excel date number, an ISO date cell, or text
// like 1/11/2026 (day first, as in New Zealand).
function asDate(value, date1904) {
  if (typeof value === 'number') {
    if (!(value > 20000 && value < 80000)) return null;
    return new Date(Date.UTC(date1904 ? 1904 : 1899, date1904 ? 0 : 11, date1904 ? 1 : 30) + Math.floor(value + 1e-9) * DAY);
  }
  if (value && typeof value === 'object' && value.iso) {
    const m = value.iso.match(/^(\d{4})-(\d{2})-(\d{2})/);
    return m ? new Date(Date.UTC(+m[1], +m[2] - 1, +m[3])) : null;
  }
  const m = text(value).match(/^(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})$/);
  if (!m) return null;
  const date = new Date(Date.UTC(+m[3], +m[2] - 1, +m[1]));
  return date.getUTCDate() === +m[1] ? date : null;
}

// "JOB009683", "J9683-CE-80", "JOB 9683" or "9683" -> "JOB009683" (null if it isn't a job number).
export function jobNumber(value) {
  const t = text(value).toUpperCase();
  const match = t.match(/^J(?:OB)?\s*0*(\d{1,6})(?=$|[^\dA-Z])/) || t.match(/^0*(\d{3,6})$/);
  return match ? `JOB${match[1].padStart(6, '0')}` : null;
}

// Hours in a cell: a number, a time (7:30 shown as a time, or typed as text), or text like
// "7.5", "7,5" or "7.5h". Anything else that isn't empty comes back as NaN (so it can be
// reported); empty cells and zeros as 0.
function cellHours(raw) {
  if (typeof raw === 'number') return raw;
  if (raw && typeof raw === 'object' && 'days' in raw) return raw.days * 24;
  if (raw == null || typeof raw === 'boolean' || typeof raw === 'object') return 0;
  const t = text(raw);
  if (!t || /^[-–—.]$/.test(t)) return 0;
  const time = t.match(/^(\d{1,2}):([0-5]\d)$/);
  if (time) return Number(time[1]) + Number(time[2]) / 60;
  const m = t.match(/^(\d+(?:[.,]\d+)?)\s*(?:h|hr|hrs|hours?)?$/i);
  return m ? Number(m[1].replace(',', '.')) : NaN;
}

const round2 = (n) => Math.round(n * 100) / 100;
const add = (into, hours) => {
  for (const [day, value] of Object.entries(hours)) into[day] = round2((into[day] || 0) + value);
};
const isTotalRow = (value) => /^(grand\s+)?total(\s+hours)?\s*:?$/i.test(value);
const isNA = (value) => /^n\/?a$/i.test(value);
const HEADING = /^(job|project)\s*(number|no\.?|#)$/i;

// The month a timesheet spreadsheet is for ("2026-11"): the one most of the dates over its
// hours fall in, else its MONTH row; '' if neither is there.
export function sheetMonth(sheet) {
  const key = (date) => `${date.getUTCFullYear()}-${String(date.getUTCMonth() + 1).padStart(2, '0')}`;
  const rows = sheet.rows;
  const headerAt = rows.findIndex((row) => row.some((cell) => HEADING.test(text(cell))));
  const counts = new Map();
  if (headerAt >= 0) {
    for (const row of [rows[headerAt], rows[headerAt - 1] || []]) {
      for (const cell of row) {
        const date = asDate(cell, sheet.date1904);
        if (date) counts.set(key(date), (counts.get(key(date)) || 0) + 1);
      }
      if (counts.size) break;
    }
  }
  if (counts.size) return [...counts].sort((a, b) => b[1] - a[1])[0][0];
  const monthRow = rows.slice(0, headerAt < 0 ? 10 : headerAt).find((row) => /^month\s*:?$/i.test(text(row[0])));
  const date = monthRow && asDate(monthRow[1], sheet.date1904);
  return date ? key(date) : '';
}

// Who the sheet says it's for: the cell beside "NAME" above the heading row, or ''.
export function sheetName(sheet) {
  const rows = sheet.rows;
  const headerAt = rows.findIndex((row) => row.some((cell) => HEADING.test(text(cell))));
  const nameRow = rows.slice(0, headerAt < 0 ? 10 : headerAt).find((row) => /^name\s*:?$/i.test(text(row[0])));
  return nameRow ? text(nameRow[1]) : '';
}

// Whether the sheet is the Business Central Job Task Overview report rather than a timesheet.
export function isJobReport(sheet) {
  return sheet.rows.slice(0, 10).some((row) => row.some((cell) => /^project task no\.?$/i.test(text(cell)))
    && row.some((cell) => /^project no\.?$/i.test(text(cell))));
}

/**
 * The month `month` ("2026-10") from a sheet read by readFirstSheet, in the saved-timesheet
 * shape ({ jobs, other }). `jobs` maps job numbers to the job list's entries (for CAPEX/REVEX
 * when the sheet doesn't say), `otherTime` lists the office's other-time names. Returns
 * { data, notes, name }: notes are things to tell the engineer; name is the sheet's NAME.
 * `choices` lists jobs open for both CAPEX and REVEX whose task the spreadsheet doesn't give:
 * [{ job, description, hours }]; their hours are left out of `data` until someone chooses
 * (see applyChoices).
 * Throws if the sheet isn't a timesheet, or is for another month (then the error's `month`
 * is the month it's for, "2026-09").
 */
export function timesheetFromSheet(sheet, month, { jobs = new Map(), otherTime = [] } = {}) {
  const [year, number] = month.split('-').map(Number);
  const days = new Date(Date.UTC(year, number, 0)).getUTCDate();
  const label = `${MONTHS[number - 1]} ${year}`;
  const rows = sheet.rows;
  const headerAt = rows.findIndex((row) => row.some((cell) => HEADING.test(text(cell))));
  if (headerAt < 0) {
    throw new Error(`The first sheet (“${sheet.name}”) has no “Job Number” heading, so it doesn’t look like a timesheet.`);
  }
  const header = rows[headerAt];
  const find = (re) => header.findIndex((cell) => re.test(text(cell)));
  const cols = {
    job: find(HEADING), location: find(/^location$/i), owner: find(/^owner$/i),
    description: find(/^description/i), task: find(/capex|revex/i), manual: find(/manual/i),
    total: find(/^(total(\s*hours?)?|hours\s+this\s+month)$/i),
  };
  const taskIn = (value) => (/capex/i.test(value) ? 'CAPEX' : /revex/i.test(value) ? 'REVEX' : '');

  // Day columns: dates in the heading row (or the row above it), else day numbers 1-31.
  const dayColumns = new Map();
  const otherMonths = new Map(); // "2026-09" -> how many of its dates
  for (const row of [header, rows[headerAt - 1] || []]) {
    row.forEach((cell, col) => {
      const date = asDate(cell, sheet.date1904);
      if (!date || dayColumns.has(col)) return;
      if (date.getUTCFullYear() === year && date.getUTCMonth() + 1 === number) dayColumns.set(col, date.getUTCDate());
      else {
        const other = `${date.getUTCFullYear()}-${String(date.getUTCMonth() + 1).padStart(2, '0')}`;
        otherMonths.set(other, (otherMonths.get(other) || 0) + 1);
      }
    });
    if (dayColumns.size) break;
  }
  if (!dayColumns.size) {
    const after = Math.max(cols.job, cols.description, cols.task, cols.manual);
    header.forEach((cell, col) => {
      if (col > after && Number.isInteger(cell) && cell >= 1 && cell <= days) dayColumns.set(col, cell);
    });
  }
  if (!dayColumns.size) {
    if (!otherMonths.size) throw new Error('The “Job Number” heading row has no dates (or day numbers) above the hours, so the days can’t be matched up.');
    const [other] = [...otherMonths].sort((a, b) => b[1] - a[1])[0];
    const [y, m] = other.split('-').map(Number);
    throw Object.assign(new Error(`This spreadsheet is for ${MONTHS[m - 1]} ${y}, not ${label}.`), { month: other });
  }

  const notes = [];
  const jobRows = new Map(); // job -> row in the saved shape
  const otherRows = new Map(); // label -> row
  const known = new Map(otherTime.map((name) => [name.toLowerCase(), name]));
  const guessed = [];
  const overruled = [];
  const choices = new Map(); // job -> { job, description, hours }: open for both, the spreadsheet doesn't say
  const unread = [];
  const dropped = [];
  const mismatched = [];
  let uncalculated = 0;
  const short = MONTHS[number - 1].slice(0, 3);
  for (let at = headerAt + 1; at < rows.length; at += 1) {
    const row = rows[at];
    const excelRow = at + 1;
    const cell = (key) => (cols[key] >= 0 ? text(row[cols[key]]) : '');
    const description = cell('description');
    const jobCell = cell('job');
    if (isTotalRow(description) || isTotalRow(jobCell)) break; // anything below the totals is a summary
    if (/daily\s*hours/i.test(description)) continue;
    const job = jobNumber(jobCell) || (!jobCell || isNA(jobCell) ? jobNumber(description) : null);
    const isOther = !job && description && (!jobCell || isNA(jobCell));
    const hours = {};
    let unreadHere = 0;
    for (const [col, day] of dayColumns) {
      if (row[col]?.formula) uncalculated += 1;
      const value = cellHours(row[col]);
      if (Number.isNaN(value)) {
        unreadHere += 1;
        if (job || isOther) unread.push(`${job || description}, ${day} ${short} (“${text(row[col])}”)`);
      } else if (round2(value) > 0) {
        hours[day] = round2((hours[day] || 0) + round2(value));
      }
    }
    const sum = round2(Object.values(hours).reduce((a, b) => a + b, 0));
    const what = job || description || jobCell;
    if (!job && !isOther) {
      // A row with hours that's neither a job nor other time: say so rather than lose them.
      const shown = [jobCell, description].filter(Boolean).join(' – ');
      if (sum > 0) dropped.push(`row ${excelRow}${shown ? ` (“${shown}”)` : ''}, ${sum} h`);
      continue;
    }
    // The sheet's own total for the row, if it has one, should match the days read.
    const raw = cols.total >= 0 ? row[cols.total] : null;
    const total = typeof raw === 'number' || raw?.days != null ? cellHours(raw) : NaN;
    const unworked = sheet.recalc && total === 0 && sum > 0; // a formula total a program never worked out
    if (!unreadHere && !unworked && Number.isFinite(total) && Math.abs(total - sum) > 0.01) {
      mismatched.push(`row ${excelRow} (“${what}”): its total is ${round2(total)} h, but its days in ${MONTHS[number - 1]} add up to ${sum} h`);
    }
    if (job) {
      // CAPEX or REVEX: a job open for just one of them in the Job Task Overview report gets that
      // one. One open for both gets what the spreadsheet says, or is left for the person loading it
      // to choose. One that isn't open gets what the spreadsheet says (else REVEX), and is flagged.
      const info = jobs.get(job);
      const open = info?.tasks || [];
      const said = taskIn(cell('manual')) || taskIn(cell('task'));
      const worked = Object.keys(hours).length > 0;
      let task = said;
      if (open.length === 1) {
        task = open[0];
        if (said && said !== task && worked) overruled.push(`${job} (the spreadsheet says ${said})`);
      } else if (!open.length && !said && worked) {
        task = 'REVEX';
        guessed.push(job);
      }
      let entry = jobRows.get(job);
      if (!entry) {
        entry = { job, location: info?.loc ?? cell('location'), owner: info?.owner ?? cell('owner'), description: info?.desc ?? description, hours: {} };
        jobRows.set(job, entry);
      }
      if (task) add(entry.hours[task] ||= {}, hours);
      else if (worked) {
        if (!choices.has(job)) choices.set(job, { job, description: entry.description, hours: {} });
        add(choices.get(job).hours, hours);
      }
      continue;
    }
    // Other time: rows marked N/A (or with no job), named in the description. Rows without
    // hours (the template's unused leave types) are left out.
    if (!Object.keys(hours).length) continue;
    const name = known.get(description.toLowerCase()) || description;
    if (!otherRows.has(name)) otherRows.set(name, { label: name, hours: {} });
    add(otherRows.get(name).hours, hours);
  }
  if (uncalculated) {
    throw new Error('Some hours in this spreadsheet are formulas that haven’t been worked out yet. Open it in Excel, save it, then load it again.');
  }
  const some = (list) => `${list.slice(0, 5).join('; ')}${list.length > 5 ? `; and ${list.length - 5} more` : ''}`;
  if (dropped.length) {
    notes.push(`${dropped.length === 1 ? 'A row has' : `${dropped.length} rows have`} hours but no job number (and isn’t marked N/A), so `
      + `${dropped.length === 1 ? 'it was' : 'they were'} left out: ${some(dropped)}. Put the job number in, or mark it N/A for other time.`);
  }
  if (mismatched.length) {
    notes.push(`${mismatched.length === 1 ? 'A row’s total doesn’t' : `${mismatched.length} rows’ totals don’t`} match the days read: ${some(mismatched)}. `
      + 'Check those hours, say ones typed in another month’s column.');
  }
  if (unread.length) {
    notes.push(`${unread.length === 1 ? 'One cell wasn’t' : `${unread.length} cells weren’t`} hours, so ${unread.length === 1 ? 'it was' : 'they were'} left out: `
      + `${some(unread)}. Type those hours in if they count.`);
  }
  if (guessed.length) {
    notes.push(`${guessed.length === 1 ? 'Job' : 'Jobs'} ${guessed.join(', ')} ${guessed.length === 1 ? 'isn’t' : 'aren’t'} open in the Job Task Overview report, and the spreadsheet doesn’t say CAPEX or REVEX, so the hours went on REVEX.`);
  }
  if (overruled.length) {
    notes.push(`The Job Task Overview report has ${overruled.length === 1 ? 'this job' : 'these jobs'} open for only one of CAPEX and REVEX, so the hours went on that one: ${overruled.join('; ')}.`);
  }
  // (Closed jobs aren't mentioned here: the timesheet page already marks them.)
  return { data: { jobs: [...jobRows.values()], other: [...otherRows.values()] }, choices: [...choices.values()], notes, name: sheetName(sheet) };
}

// The month with the CAPEX/REVEX choices made: picks maps job -> 'CAPEX' or 'REVEX'. Jobs not
// picked yet keep their hours out.
export function applyChoices(data, choices = [], picks = {}) {
  const jobsOut = data.jobs.map((entry) => ({ ...entry, hours: Object.fromEntries(Object.entries(entry.hours).map(([t, h]) => [t, { ...h }])) }));
  for (const choice of choices) {
    const task = picks[choice.job];
    const entry = jobsOut.find((e) => e.job === choice.job);
    if (entry && (task === 'CAPEX' || task === 'REVEX')) add(entry.hours[task] ||= {}, choice.hours);
  }
  return { ...data, jobs: jobsOut };
}
