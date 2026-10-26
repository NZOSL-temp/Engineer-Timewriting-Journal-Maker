// Shared code for the timewriting website: sign-in, the encrypted job list,
// storage on this computer, and saving timesheets to GitHub. No build step.

// Refuse to run inside someone else's frame (clickjacking); GitHub Pages can't send
// a frame-ancestors header, so styles.css hides framed pages and this tries to break out.
if (window.top !== window.self) {
  document.documentElement.classList.add('framed');
  try {
    window.top.location.replace(window.location.href);
  } catch {
    /* the framing page blocked it; the page stays hidden */
  }
  throw new Error('This page can’t be shown inside another website.');
}

export const TASKS = ['CAPEX', 'REVEX'];
export const MARKER = '<!-- etjm:timesheet:v2 -->';
const PASSWORD_MARKER = '<!-- etjm:password:v1 -->';
const SESSION_HOURS = 12;
const REMEMBER_DAYS = 30;
const KEYS = {
  session: 'etjm.session.v4',
  lastName: 'etjm.lastName.v1',
  draft: 'etjm.draft.v4.',
  issue: 'etjm.issue.v4.',
};

// Timesheet copies kept by earlier versions of the site (from testing, before the data was
// started again from September on 18, 19 and 20 Oct 2026) are deleted, so nobody sees months
// the office no longer has.
try {
  const old = [];
  for (let i = 0; i < localStorage.length; i += 1) {
    const key = localStorage.key(i);
    if (/^etjm\.(draft|issue)\.v[1-3]\./.test(key || '')) old.push(key);
  }
  old.forEach((key) => localStorage.removeItem(key));
} catch {
  /* storage unavailable */
}
// Filled in when the site is deployed (.github/workflows/pages.yml); left as it is locally.
const VERSION = '__ETJM_VERSION__';
const BUNDLE_URL = 'data/bundle.enc.json';
const USERS_URL = 'data/users.json';
const API = 'https://api.github.com';
const utf8 = new TextEncoder();
const fromUtf8 = new TextDecoder();

export class AppError extends Error {
  constructor(code, message, status) {
    super(message);
    this.code = code;
    this.status = status;
  }
}

// ------------------------------------------------------------------ DOM bits

export const $ = (selector, root = document) => root.querySelector(selector);

export function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'dataset') Object.assign(node.dataset, value);
    else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
    else if (key in node && !key.includes('-')) node[key] = value;
    else node.setAttribute(key, value === true ? '' : value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

let toastTimer;
export function toast(message, { tone = 'info', action, duration = 4500 } = {}) {
  let box = $('#toast');
  if (!box) {
    box = el('div', { id: 'toast', class: 'toast', role: 'status', 'aria-live': 'polite' });
    document.body.append(box);
  }
  box.replaceChildren(el('span', {}, message));
  if (action) box.append(el('button', { class: 'toast-action', type: 'button', onclick: () => { hide(); action.run(); } }, action.label));
  box.dataset.tone = tone;
  box.classList.add('show');
  clearTimeout(toastTimer);
  const hide = () => box.classList.remove('show');
  toastTimer = setTimeout(hide, duration);
}

// If the website is updated while someone has it open, offer to reload it. The check runs
// when they come back to the tab and every few minutes; nothing is lost on reload, because
// timesheets are kept on this computer as they're typed.
function watchForUpdates() {
  if (VERSION.startsWith('__')) return;
  let told = false;
  const check = async () => {
    if (told || document.hidden) return;
    try {
      const { version } = await (await fetch('version.json', { cache: 'no-store' })).json();
      if (version && version !== VERSION) {
        told = true;
        toast('This website has been updated.', { action: { label: 'Reload', run: () => location.reload() }, duration: 60 * 60 * 1000 });
      }
    } catch {
      /* offline, or an older deploy without version.json */
    }
  };
  document.addEventListener('visibilitychange', check);
  setInterval(check, 5 * 60 * 1000);
  setTimeout(check, 20 * 1000);
}
watchForUpdates();

// --------------------------------------------------------------------- text

export function cleanText(value) {
  return String(value ?? '')
    .normalize('NFC')
    .replace(/[\p{Cc}\p{Cf}]/gu, (ch) => (/\s/.test(ch) ? ' ' : ''))
    .replace(/\s+/g, ' ')
    .trim();
}

export function nameProblem(name) {
  if (!name) return 'Please enter your name.';
  if (name.length > 60) return 'That name is longer than 60 characters.';
  if (!/^\p{L}[\p{L}\p{M} .'’-]*$/u.test(name)) {
    return 'Use letters, spaces, apostrophes, hyphens and full stops only, starting with a letter.';
  }
  return null;
}

export const nameKey = (name) => cleanText(name).toLowerCase();
export const normalizeUsername = (username) => String(username || '').normalize('NFC').trim().toLowerCase();
// Passwords ignore case, spaces and dashes, so "abcd efgh jkmn" works for ABCD-EFGH-JKMN.
export const normalizePassword = (password) => String(password || '').normalize('NFC').toUpperCase().replace(/[\s-]+/g, '');

export function fmtHours(value) {
  const rounded = Math.round((Number(value) || 0) * 100) / 100;
  return String(rounded);
}

// "7", "7.5", "7,5" and "7:30" are all seven and a half hours. Blank is 0.
export function parseHours(text) {
  const raw = String(text ?? '').trim().replace(',', '.');
  if (raw === '') return { ok: true, value: 0 };
  let value;
  const clock = raw.match(/^(\d{1,2}):([0-5]\d)$/);
  if (clock) value = Number(clock[1]) + Number(clock[2]) / 60;
  else if (/^(\d{1,2}(\.\d*)?|\.\d+)$/.test(raw)) value = Number(raw);
  else return { ok: false, message: 'Enter hours as a number, e.g. 7.5' };
  value = Math.round(value * 100) / 100;
  if (!(value >= 0 && value <= 24)) return { ok: false, message: 'Hours for one day must be between 0 and 24' };
  return { ok: true, value };
}

// -------------------------------------------------------------------- months

export function thisMonth() {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}`;
}

export function shiftMonth(month, delta) {
  const [y, m] = month.split('-').map(Number);
  const d = new Date(y, m - 1 + delta, 1);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
}

export function monthInfo(month) {
  const [y, m] = month.split('-').map(Number);
  const days = new Date(y, m, 0).getDate();
  const label = new Date(y, m - 1, 1).toLocaleDateString('en-NZ', { month: 'long', year: 'numeric' });
  const end = new Date(y, m, 0);
  return { key: month, y, m, days, label, end };
}

export const isMonth = (value) => /^\d{4}-(0[1-9]|1[0-2])$/.test(value || '');

export function fmtDateTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  return d.toLocaleString('en-NZ', { day: 'numeric', month: 'short', hour: 'numeric', minute: '2-digit' });
}

// ------------------------------------------------------- storage on this PC

export const store = {
  get(key, area = 'local') {
    try {
      const raw = window[`${area}Storage`].getItem(key);
      return raw === null ? null : JSON.parse(raw);
    } catch {
      return null;
    }
  },
  set(key, value, area = 'local') {
    try {
      window[`${area}Storage`].setItem(key, JSON.stringify(value));
      return true;
    } catch {
      return false;
    }
  },
  remove(key, area = 'local') {
    try {
      window[`${area}Storage`].removeItem(key);
    } catch {
      /* storage unavailable */
    }
  },
};

export const lastName = {
  get: () => store.get(KEYS.lastName) || '',
  set: (name) => store.set(KEYS.lastName, name),
};

// Each person's months are kept on this computer as they type, keyed by username.
export const drafts = {
  key: (username, month) => `${KEYS.draft}${username}.${month}`,
  get(username, month) {
    return store.get(this.key(username, month));
  },
  set(username, month, record) {
    return store.set(this.key(username, month), record);
  },
  remove(username, month) {
    store.remove(this.key(username, month));
  },
  // Signing out removes this person's copies that are already saved to the office;
  // copies with unsaved changes stay, so nothing is lost. Returns how many stayed.
  forgetSaved(username) {
    let kept = 0;
    for (const month of this.months(username)) {
      const record = this.get(username, month);
      if (record?.meta?.dirty) kept += 1;
      else store.remove(this.key(username, month));
    }
    return kept;
  },
  months(username) {
    const prefix = `${KEYS.draft}${username}.`;
    const found = [];
    try {
      for (let i = 0; i < localStorage.length; i += 1) {
        const key = localStorage.key(i);
        if (key?.startsWith(prefix) && isMonth(key.slice(prefix.length))) found.push(key.slice(prefix.length));
      }
    } catch {
      /* storage unavailable */
    }
    return found;
  },
};

// ------------------------------------------------------------------ session

// A session lasts 12 hours, or 30 days with "Keep me signed in". It holds the person's
// user key, which unlocks their profile until an admin resets them.
export function saveSession(session) {
  clearSession();
  const hours = session.remember ? REMEMBER_DAYS * 24 : SESSION_HOURS;
  store.set(KEYS.session, { exp: Date.now() + hours * 3600 * 1000, ...session }, session.remember ? 'local' : 'session');
}

export function loadSession() {
  const session = store.get(KEYS.session, 'session') || store.get(KEYS.session, 'local');
  if (session && !(session.exp > Date.now())) {
    clearSession();
    return null;
  }
  return session;
}

export function clearSession() {
  store.remove(KEYS.session, 'session');
  store.remove(KEYS.session, 'local');
}

// ------------------------------------------------------------ signing in
//
// data/users.json has, for each person (under a hash of their username), their user key
// locked with their password, and their profile locked with their user key. The profile
// holds their name, rate and keys: the job-list key, their timesheet keys and, for
// admins, the admin token. Nobody else's sign-in can open it.

export const b64ToBytes = (b64) => Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
const b64urlToBytes = (text) => b64ToBytes(text.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - (text.length % 4)) % 4));

export function bytesToB64(bytes) {
  let binary = '';
  for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(binary);
}
const bytesToB64url = (bytes) => bytesToB64(bytes).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');

async function fetchJSON(url, what) {
  let response;
  try {
    response = await fetch(url, { cache: 'no-cache' });
  } catch {
    throw new AppError('network', 'Couldn’t reach the website. Check your connection and try again.');
  }
  if (response.status === 404) throw new AppError('missing', `The ${what} hasn’t been published yet. Please ask the office.`);
  if (!response.ok) throw new AppError('network', `Couldn’t load the ${what} (error ${response.status}).`);
  return response.json();
}

async function sha256Hex(text) {
  const digest = new Uint8Array(await crypto.subtle.digest('SHA-256', utf8.encode(text)));
  return [...digest].map((b) => b.toString(16).padStart(2, '0')).join('');
}

// AES-256-GCM as "<iv>.<ciphertext>" (base64url), with associated data saying what it is
// and whose it is. The same format as seal()/unseal() in tools/etjm_core.py.
async function seal(keyBytes, bytes, aad) {
  const key = await crypto.subtle.importKey('raw', keyBytes, 'AES-GCM', false, ['encrypt']);
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const ct = new Uint8Array(await crypto.subtle.encrypt({ name: 'AES-GCM', iv, additionalData: utf8.encode(aad) }, key, bytes));
  return `${bytesToB64url(iv)}.${bytesToB64url(ct)}`;
}

async function unseal(keyBytes, token, aad) {
  const [iv, ct] = String(token).split('.');
  const key = await crypto.subtle.importKey('raw', keyBytes, 'AES-GCM', false, ['decrypt']);
  return new Uint8Array(await crypto.subtle.decrypt({ name: 'AES-GCM', iv: b64urlToBytes(iv), additionalData: utf8.encode(aad) }, key, b64urlToBytes(ct)));
}

async function passwordKey(password, saltB64, iterations) {
  const base = await crypto.subtle.importKey('raw', utf8.encode(normalizePassword(password)), 'PBKDF2', false, ['deriveBits']);
  return new Uint8Array(await crypto.subtle.deriveBits({ name: 'PBKDF2', hash: 'SHA-256', salt: b64ToBytes(saltB64), iterations }, base, 256));
}

export const SIGN_IN_AGAIN = {
  again: 'Please sign in again.',
  expired: 'You were signed out after a while. Please sign in again.',
  removed: 'Your sign-in has been removed. Please ask the office.',
  reset: 'Your sign-in has been reset by the office. Please sign in with your new password.',
  updated: 'The website has just been updated. Please sign in again.',
};
const signInAgain = (reason = 'again') => Object.assign(new AppError('signin', SIGN_IN_AGAIN[reason]), { reason });
const badLogin = () => new AppError('bad-login', 'That username and password don’t match. Check them, or ask the office.');

async function userEntry(username) {
  const users = await fetchJSON(USERS_URL, 'sign-in list');
  return users.users?.[await sha256Hex(`etjm-user:${normalizeUsername(username)}`)] || null;
}

async function openProfile(entry, userKey, username) {
  const profile = JSON.parse(fromUtf8.decode(await unseal(userKey, entry.profile, `etjm-profile|${username}`)));
  const blob = await fetchJSON(BUNDLE_URL, 'job list');
  if (blob.epoch !== profile.epoch) throw signInAgain('updated');
  const bundle = JSON.parse(fromUtf8.decode(await unseal(b64ToBytes(profile.bundleKey), blob.data, `etjm-bundle|${blob.epoch}`)));
  return { bundle, profile };
}

export async function signIn(username, password) {
  if (!globalThis.crypto?.subtle) throw new AppError('browser', 'This browser can’t open the timesheets. Please use an up-to-date Chrome, Edge, Safari or Firefox.');
  const name = normalizeUsername(username);
  const entry = await userEntry(name);
  if (!entry?.profile) throw badLogin();
  let userKey;
  try {
    userKey = await unseal(await passwordKey(password, entry.salt, entry.iterations), entry.key, `etjm-user-key|${name}`);
  } catch {
    throw badLogin();
  }
  const opened = await openProfile(entry, userKey, name);
  return { ...opened, session: { username: name, name: opened.profile.name, uk: bytesToB64(userKey) } };
}

// Open the latest bundle and profile for a signed-in session.
export async function openSession(session) {
  if (!session?.uk) throw signInAgain();
  const entry = await userEntry(session.username);
  if (!entry?.profile) throw signInAgain('removed');
  let opened;
  try {
    opened = await openProfile(entry, b64ToBytes(session.uk), session.username);
  } catch (error) {
    if (error instanceof AppError) throw error;
    throw signInAgain('reset');
  }
  if (opened.profile.name !== session.name) saveSession({ ...session, name: opened.profile.name });
  return opened;
}

// Change your own password. The request is sealed with your user key, so only you can
// make it; the office never sees the new password. Resolves once the website has it.
export async function changePassword(bundle, profile, session, newPassword) {
  const salt = crypto.getRandomValues(new Uint8Array(16));
  const iterations = 600000;
  const saltB64 = bytesToB64(salt);
  const userKey = b64ToBytes(session.uk);
  const key = await seal(await passwordKey(newPassword, saltB64, iterations), userKey, `etjm-user-key|${session.username}`);
  const request = { seq: Date.now(), wrap: { salt: saltB64, iterations, key } };
  const token = await seal(userKey, utf8.encode(JSON.stringify(request)), `etjm-pw|${session.username}`);
  const body = [PASSWORD_MARKER, 'A password change, encrypted. Only the timewriting system can apply it.', '', '```etjm', `ETJMP1.${token}`, '```', ''].join('\n');
  await github(bundle, 'POST', `/repos/${bundle.dataRepo}/issues`, { title: `Password change · ${profile.tag}`, body });
  const deadline = Date.now() + 4 * 60 * 1000;
  while (Date.now() < deadline) {
    await new Promise((resolve) => { setTimeout(resolve, 6000); });
    const entry = await userEntry(session.username).catch(() => null);
    if (entry?.salt === saltB64) return true;
  }
  return false;
}

// Check someone's current password (before letting them change it).
export async function checkPassword(username, password) {
  const entry = await userEntry(username);
  try {
    await unseal(await passwordKey(password, entry.salt, entry.iterations), entry.key, `etjm-user-key|${username}`);
    return true;
  } catch {
    return false;
  }
}

// What an admin needs to show the passwords the office issued.
export async function issuedPassword(profile, username, token) {
  return fromUtf8.decode(await unseal(b64ToBytes(profile.passwordKey), token, `password|${username}`));
}

// Check a password someone chooses (the same rules as check_own_password in Python).
export function passwordProblem(password, username) {
  const plain = normalizePassword(password);
  if (plain.length < 12) return 'Use at least 12 characters. A few words with spaces works well.';
  if (new Set(plain).size < 5) return 'Use a mix of different characters.';
  if (plain.includes(String(username).replace(/\./g, '').toUpperCase())) return 'Don’t include your username.';
  return null;
}

// The eye next to a password box shows what's typed only while it's held down (with the
// mouse, a finger, or Space or Enter), and hides it again the moment it's let go.
export function holdToShow(button, inputs) {
  const set = (show) => {
    for (const input of inputs) input.type = show ? 'text' : 'password';
    button.classList.toggle('showing', show);
  };
  const hide = () => set(false);
  button.addEventListener('pointerdown', (event) => {
    if (event.button !== 0) return;
    event.preventDefault(); // keeps the cursor in the password box
    button.setPointerCapture?.(event.pointerId);
    set(true);
  });
  for (const type of ['pointerup', 'pointercancel', 'lostpointercapture', 'blur']) button.addEventListener(type, hide);
  button.addEventListener('keydown', (event) => {
    if (event.key !== ' ' && event.key !== 'Enter') return;
    event.preventDefault();
    if (!event.repeat) set(true);
  });
  button.addEventListener('keyup', (event) => {
    if (event.key === ' ' || event.key === 'Enter') hide();
  });
  button.addEventListener('click', (event) => event.preventDefault());
  button.addEventListener('contextmenu', (event) => event.preventDefault()); // a long press on a phone
  window.addEventListener('blur', hide);
  return hide;
}

// ------------------------------------------------------- forgotten passwords
//
// "Forgot your password?" asks the office's reset helper (relay/Code.gs) for an email.
// The data repo's Action then makes new keys for that person and emails a link holding a
// secret, after the # so it never reaches a web server. The secret opens only a copy of the
// new user key, published next to their sign-in, and a short-lived profile with just enough
// to send a new password (see etjm_accounts.py). Their old password works until then.

export async function resetHelper() {
  const users = await fetchJSON(USERS_URL, 'sign-in list');
  return typeof users.reset?.url === 'string' && users.reset.url.startsWith('https://') ? users.reset.url : null;
}

// Ask for a reset email. It says nothing about whether the username exists.
export async function requestReset(username) {
  const url = await resetHelper();
  if (!url) throw new AppError('no-reset', 'Password reset by email isn’t switched on yet. Ask the office for a new password.');
  const uid = await sha256Hex(`etjm-user:${normalizeUsername(username)}`);
  try {
    // "no-cors": the answer is the same whatever happens, so there's nothing to read.
    await fetch(url, {
      method: 'POST', mode: 'no-cors', credentials: 'omit', cache: 'no-store', redirect: 'follow',
      headers: { 'Content-Type': 'text/plain;charset=utf-8' }, body: JSON.stringify({ action: 'request', uid }),
    });
  } catch {
    throw new AppError('network', 'Couldn’t send the request. Check your connection and try again.');
  }
}

async function recoverKey(secret) {
  const base = await crypto.subtle.importKey('raw', secret, 'HKDF', false, ['deriveBits']);
  return new Uint8Array(await crypto.subtle.deriveBits(
    { name: 'HKDF', hash: 'SHA-256', salt: new Uint8Array(32), info: utf8.encode('etjm/recover') }, base, 256));
}

// What a reset link ({ u, id, k } from the email) opens. status is 'ok', or 'missing' (not on
// the website yet, or used up), 'replaced' (a newer link was sent), 'expired', 'updating'
// (the website is changing over: try again shortly) or 'bad' (the link isn't complete).
export async function openResetLink(link) {
  const username = normalizeUsername(link?.u);
  if (!username || !/^[0-9a-f]{16}$/.test(link?.id || '') || !/^[A-Za-z0-9_-]{43}$/.test(link?.k || '')) return { status: 'bad' };
  const recover = (await userEntry(username))?.recover;
  if (!recover) return { status: 'missing', username };
  if (recover.id !== link.id) return { status: 'replaced', username };
  if (!(Date.parse(recover.expires) > Date.now())) return { status: 'expired', username };
  let userKey;
  let profile;
  try {
    userKey = await unseal(await recoverKey(b64urlToBytes(link.k)), recover.key, `etjm-recover|${username}|${link.id}`);
    profile = JSON.parse(fromUtf8.decode(await unseal(userKey, recover.profile, `etjm-recover-profile|${username}|${link.id}`)));
  } catch {
    return { status: 'bad', username };
  }
  const blob = await fetchJSON(BUNDLE_URL, 'job list');
  if (blob.epoch !== profile.epoch) return { status: 'updating', username };
  const bundle = JSON.parse(fromUtf8.decode(await unseal(b64ToBytes(profile.bundleKey), blob.data, `etjm-bundle|${blob.epoch}`)));
  return { status: 'ok', username, profile, bundle, userKey, expires: recover.expires };
}

// Send the new password from a reset link, sealed with the link's new user key. Resolves
// true once the website has it (about a minute).
export async function finishReset(opened, newPassword) {
  const { username, profile, bundle, userKey } = opened;
  if (!bundle.saveToken) throw new AppError('no-save', 'Saving to the office isn’t switched on yet, so passwords can’t be reset. Ask the office.');
  const saltB64 = bytesToB64(crypto.getRandomValues(new Uint8Array(16)));
  const iterations = 600000;
  const key = await seal(await passwordKey(newPassword, saltB64, iterations), userKey, `etjm-user-key|${username}`);
  const request = { id: profile.recover, wrap: { salt: saltB64, iterations, key } };
  const token = await seal(userKey, utf8.encode(JSON.stringify(request)), `etjm-recover-pw|${username}|${profile.recover}`);
  const body = [PASSWORD_MARKER, 'A password reset, encrypted. Only the timewriting system can apply it.', '', '```etjm', `ETJMR1.${token}`, '```', ''].join('\n');
  await github(bundle, 'POST', `/repos/${bundle.dataRepo}/issues`, { title: `Password change · ${profile.tag}`, body });
  const deadline = Date.now() + 4 * 60 * 1000;
  while (Date.now() < deadline) {
    await new Promise((resolve) => { setTimeout(resolve, 6000); });
    const entry = await userEntry(username).catch(() => null);
    if (entry?.salt === saltB64) return true;
  }
  return false;
}

// Where each kind of person starts after signing in.
export const homeFor = (profile) => (profile.timesheets ? 'home.html' : 'admin.html');

// ---------------------------------------------------------- the journal

const MONTH_NAMES = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];
const MONTH_ABBR = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const pad2 = (n) => String(n).padStart(2, '0');

export const JOURNAL_HEADER = ['Employee - ID', 'Date', 'GST inclusive ', 'GST', 'Excl', 'GL', 'Project', 'Description',
  'Capex/Revex', 'No. of hours', 'Recharge Rate', 'Document number'];
export const YELLOW_COLUMNS = new Set(['Project', 'Capex/Revex', 'No. of hours']);

function strftime(format, date) {
  const parts = {
    d: pad2(date.getDate()), m: pad2(date.getMonth() + 1), Y: String(date.getFullYear()),
    y: pad2(date.getFullYear() % 100), B: MONTH_NAMES[date.getMonth()], b: MONTH_ABBR[date.getMonth()], '%': '%',
  };
  return format.replace(/%([dmYyBb%])/g, (_, c) => parts[c]);
}

const fill = (template, tokens) => template.replace(/\{(\w+)\}/g, (match, key) => (key in tokens ? tokens[key] : match));

// The journal lines for one timesheet, worked out the same way as the Action
// (tools/etjm_outputs.py): one line per job and Capex/Revex with hours, then a
// balancing line. Amounts are in cents; rate × hours rounded half up.
export function journalLines(data, rate, journal) {
  const { y, m, end } = monthInfo(data.month);
  const tokens = { employee: data.employee, monthLabel: journal.monthLabels[m - 1], yy: pad2(y % 100), yyyy: String(y), mon: MONTH_ABBR[m - 1], month: MONTH_NAMES[m - 1] };
  const date = strftime(journal.dateFormat, end);
  const description = fill(journal.description, tokens);
  const document = fill(journal.documentNumber, { ...tokens, employee: '' });
  const rateCents = rate == null ? null : Math.round(Number(rate) * 100);
  const lines = [];
  let total = 0;
  for (const job of data.jobs || []) {
    for (const task of TASKS) {
      const hundredths = Math.round(Object.values(job.hours?.[task] || {}).reduce((a, b) => a + Number(b), 0) * 100);
      if (hundredths <= 0) continue;
      const amount = rateCents == null ? null : Math.round((rateCents * hundredths) / 100);
      total += amount || 0;
      lines.push({ employee: data.employee, date, amount, gl: '', project: job.job, description, task, hours: hundredths / 100, rate, document });
    }
  }
  if (lines.length) {
    lines.push({ employee: data.employee, date, amount: rateCents == null ? null : -total, gl: String(journal.balancingGlAccount), project: '', description, task: '', hours: null, rate: null, document, balancing: true });
  }
  return lines;
}

export const fmtMoney = (cents) => (cents == null ? '' : ((cents || 0) / 100).toLocaleString('en-NZ', { minimumFractionDigits: 2, maximumFractionDigits: 2 }));

// The twelve journal columns for one line, as shown in the spreadsheet.
export function journalCells(line) {
  return [line.employee, line.date, fmtMoney(line.amount), line.amount == null ? '' : '0.00', fmtMoney(line.amount), line.gl,
    line.project, line.description, line.task, line.hours == null ? '' : fmtHours(line.hours), line.rate == null ? '' : String(line.rate), line.document];
}

// The journal as a table, laid out like the Journal sheet (yellow input columns).
export function journalTable(rows) {
  const numeric = new Set([2, 3, 4, 9, 10]);
  const cls = (i) => [YELLOW_COLUMNS.has(JOURNAL_HEADER[i]) && 'yellow', numeric.has(i) && 'num'].filter(Boolean).join(' ');
  return el('div', { class: 'journal-wrap' }, el('table', { class: 'journal-table' },
    el('thead', {}, el('tr', {}, JOURNAL_HEADER.map((name, i) => el('th', { class: cls(i), scope: 'col' }, name)))),
    el('tbody', {}, rows.map(({ cells, balancing }) => el('tr', { class: balancing ? 'balancing' : '' },
      cells.map((cell, i) => el('td', { class: cls(i) }, cell)))))));
}

// ------------------------------------------------------------------ GitHub

async function request(token, method, path, body, { raw = false, messages = {} } = {}) {
  let response;
  try {
    response = await fetch(API + path, {
      method,
      cache: 'no-store',
      headers: {
        Authorization: `Bearer ${token}`,
        Accept: raw ? 'application/vnd.github.raw+json' : 'application/vnd.github+json',
        ...(body ? { 'Content-Type': 'application/json' } : {}),
      },
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new AppError('network', 'Couldn’t reach GitHub. Check your connection and try again.');
  }
  if (!response.ok) {
    const message = messages[response.status] || `GitHub had a problem (error ${response.status}). Try again in a minute.`;
    throw new AppError('github', message, response.status);
  }
  if (response.status === 204) return null;
  return raw ? response.blob() : response.json();
}

const SAVE_MESSAGES = {
  401: 'The website’s save key has expired or been removed. Your hours are still on this computer. Please tell the office.',
  403: 'GitHub refused the save (the save key may not have permission, or there were too many saves). Try again in a minute.',
  404: 'GitHub couldn’t find the timesheet store. Please tell the office.',
  422: 'GitHub didn’t accept the timesheet. Please tell the office.',
};

const github = (bundle, method, path, body) => request(bundle.saveToken, method, path, body, { messages: SAVE_MESSAGES });

const ADMIN_MESSAGES = {
  401: 'The admin key has expired or been removed. Renew the ADMIN_TOKEN secret in the data repo.',
  403: 'GitHub refused (the admin key may not have permission, or there were too many requests). Try again in a minute.',
  409: 'Someone else changed the same file at the same moment. Try again.',
  422: 'GitHub didn’t accept the upload. Try again in a minute.',
};
const encodePath = (path) => path.split('/').map(encodeURIComponent).join('/');

// What the admin page can do in the private data repo (needs the admin token in the profile).
export function adminRepo(bundle, profile) {
  const repo = bundle.dataRepo;
  const call = (method, path, body, options = {}) => request(profile.adminToken, method, path, body, { ...options, messages: ADMIN_MESSAGES });
  const missingOk = async (promise, fallback) => {
    try {
      return await promise;
    } catch (error) {
      if (error.status === 404) return fallback;
      throw error;
    }
  };
  // Every file in the data repo is encrypted with the store key (only admins' profiles have
  // it), bound to its path: {"etjm": "encrypted", "v": 1, "data": "<iv>.<ct>"}. The same
  // format as tools/etjm_vault.py.
  const storeKey = profile.storeKey ? b64ToBytes(profile.storeKey) : null;
  const open = async (path, text) => {
    if (!storeKey) throw new AppError('admin', 'Your sign-in can’t open the office’s files. Sign out and in again.');
    let envelope = null;
    try {
      envelope = JSON.parse(text);
    } catch { /* not JSON */ }
    if (envelope?.etjm !== 'encrypted') throw new AppError('admin', `${path} in the data repo isn’t encrypted, so it was ignored.`);
    try {
      return await unseal(storeKey, envelope.data, `etjm-file|${path}`);
    } catch {
      throw new AppError('admin', `${path} in the data repo couldn’t be decrypted: it may have been changed.`);
    }
  };
  const lock = async (path, bytes) => `${JSON.stringify({ etjm: 'encrypted', v: 1, data: await seal(storeKey, bytes, `etjm-file|${path}`) })}\n`;
  const raw = (path) => call('GET', `/repos/${repo}/contents/${encodePath(path)}`, null, { raw: true });
  return {
    file: (path) => missingOk(call('GET', `/repos/${repo}/contents/${encodePath(path)}`), null),
    list: (path) => missingOk(call('GET', `/repos/${repo}/contents/${encodePath(path)}`), []),
    runs: (sha) => call('GET', `/repos/${repo}/actions/runs?head_sha=${sha}&per_page=10`),
    // A file's decrypted bytes, or null if it doesn't exist.
    async read(path) {
      const blob = await missingOk(raw(path), null);
      return blob && open(path, await blob.text());
    },
    // An encrypted JSON file and its sha (needed to save it back), or null if it doesn't exist.
    async readJson(path) {
      const file = await missingOk(call('GET', `/repos/${repo}/contents/${encodePath(path)}`), null);
      if (!file) return null;
      const text = file.content ? fromUtf8.decode(b64ToBytes(file.content.replace(/\s/g, ''))) : await (await raw(path)).text();
      return { data: JSON.parse(fromUtf8.decode(await open(path, text))), sha: file.sha };
    },
    // Encrypt and save a file; returns the commit and the file's new sha.
    async write(path, bytes, message, sha) {
      const result = await call('PUT', `/repos/${repo}/contents/${encodePath(path)}`,
        { message, content: bytesToB64(utf8.encode(await lock(path, bytes))), ...(sha ? { sha } : {}) });
      return { commit: result.commit.sha, sha: result.content.sha };
    },
    writeJson(path, data, message, sha) {
      return this.write(path, utf8.encode(`${JSON.stringify(data, null, 1)}\n`), message, sha);
    },
  };
}

// --------------------------------------------------------------- viewing as
//
// "View" on the Admin page shows an engineer's pages as they see them, read-only. It uses
// each month's last verified save, which the Actions keep (encrypted) in the data repo and
// the journals are built from, so admins see nothing new. Nothing can be changed: saving
// needs the engineer's own timesheet key, which admins don't have. Hours an engineer has
// typed but not saved are only on their computer, so they don't show.

export const viewAsParam = () => normalizeUsername(new URLSearchParams(location.search).get('as') || '');

export async function openViewAs(bundle, profile, username) {
  if (!profile.admin || !profile.adminToken || !profile.storeKey) {
    throw new AppError('view-as', 'Only the office can look at someone else’s timesheets.');
  }
  const repo = adminRepo(bundle, profile);
  const settings = (await repo.readJson('settings.json'))?.data || {};
  const employee = (settings.employees || []).find((e) => normalizeUsername(e.username || defaultUsername(e.name)) === username);
  if (!employee) throw new AppError('view-as', `There’s no engineer with the username ${username}.`);
  const files = (await repo.list('timesheets')).filter((f) => f.type === 'file' && f.name.endsWith('.json'));
  const months = new Map();
  for (const file of files) {
    const month = JSON.parse(fromUtf8.decode(await repo.read(file.path)));
    const theirs = (month.timesheets || []).find((t) => t.username === username && !t.deleted);
    if (theirs && isMonth(month.month)) months.set(month.month, { month: month.month, savedAt: theirs.savedAt, data: theirs });
  }
  return { person: { name: employee.name, username, rate: employee.rechargeRate ?? null, active: employee.active !== false }, months };
}

// The username settings.json gives someone by default ("Alex Smith" -> "alex.smith"),
// the same as default_username() in tools/etjm_core.py.
export function defaultUsername(name) {
  return String(name || '').normalize('NFKD').replace(/[^\x00-\x7f]/g, '').toLowerCase()
    .replace(/[^a-z0-9]+/g, '.').replace(/^\.+|\.+$/g, '');
}

// Timesheets are GitHub issues titled "Timesheet 2026-10 · <tag>": the tag is a random-
// looking label for the person, so titles don't name anyone. The body is encrypted with
// the person's newest timesheet key; anyone else who can see the issues (everyone who
// can sign in shares the save token) can't read it, and can't change it unnoticed.

export const issueTitle = (profile, month) => `Timesheet ${month} · ${profile.tag}`;
const issueCacheKey = (bundle, profile, month) => `${KEYS.issue}${bundle.dataRepo}.${profile.tag}.${month}`;

function cipherToken(body, marker) {
  const after = (body || '').split(marker)[1];
  const match = after && after.match(/```etjm[ \t]*\r?\n([A-Za-z0-9_.-]+)\r?\n```/);
  return match ? match[1] : null;
}

const isOurs = (issue, title) => issue && !issue.pull_request && issue.title.trim() === title && (issue.body || '').includes(MARKER);

// The saved data in an issue, or { unverified: true } if it can't be opened with this
// person's keys (someone else changed it).
async function readIssue(issue, profile, username, month) {
  const parts = (cipherToken(issue.body, MARKER) || '').split('.');
  const key = profile.dataKeys?.[Number(parts[1])];
  if (parts[0] !== 'ETJM2' || !key) return { unverified: true };
  try {
    const data = JSON.parse(fromUtf8.decode(await unseal(b64ToBytes(key), parts.slice(2).join('.'), `etjm-ts|${username}|${month}`)));
    return data.month === month ? { data } : { unverified: true };
  } catch {
    return { unverified: true };
  }
}

async function findIssue(bundle, profile, month) {
  const title = issueTitle(profile, month);
  const cacheKey = issueCacheKey(bundle, profile, month);
  const cached = store.get(cacheKey);
  if (cached) {
    try {
      const issue = await github(bundle, 'GET', `/repos/${bundle.dataRepo}/issues/${cached}`);
      if (isOurs(issue, title)) return issue;
    } catch (error) {
      if (![404, 410].includes(error.status)) throw error;
    }
    store.remove(cacheKey);
  }
  const query = `repo:${bundle.dataRepo} is:issue in:title "${title}"`;
  const result = await github(bundle, 'GET', `/search/issues?q=${encodeURIComponent(query)}&sort=created&order=asc&per_page=20`);
  const match = (result.items || []).find((issue) => isOurs(issue, title));
  if (!match) return null;
  store.set(cacheKey, match.number);
  return github(bundle, 'GET', `/repos/${bundle.dataRepo}/issues/${match.number}`);
}

function totals(data) {
  let jobs = 0;
  let other = 0;
  for (const job of data?.jobs || []) for (const task of TASKS) jobs += Object.values(job.hours?.[task] || {}).reduce((a, b) => a + Number(b), 0);
  for (const row of data?.other || []) other += Object.values(row.hours || {}).reduce((a, b) => a + Number(b), 0);
  return { jobs, other };
}
export { totals as sheetTotals };

// Every month this person has saved to the office (that could be opened with their keys).
export async function listSaved(bundle, profile, username) {
  const query = `repo:${bundle.dataRepo} is:issue in:title "Timesheet" "${profile.tag}"`;
  const result = await github(bundle, 'GET', `/search/issues?q=${encodeURIComponent(query)}&sort=created&order=desc&per_page=100`);
  const byMonth = new Map();
  for (const issue of result.items || []) {
    const match = issue.title.trim().match(/^Timesheet (\d{4}-\d{2}) · ([0-9a-f]{16})$/);
    if (!match || match[2] !== profile.tag || byMonth.has(match[1]) || !isOurs(issue, issue.title.trim())) continue;
    const { data } = await readIssue(issue, profile, username, match[1]);
    if (data?.deleted) byMonth.set(match[1], null); // deleted by them: not listed
    else if (data) byMonth.set(match[1], { month: match[1], savedAt: data.savedAt, issue: issue.number, data });
  }
  return [...byMonth.values()].filter(Boolean);
}

export async function loadSaved(bundle, profile, username, month) {
  const issue = await findIssue(bundle, profile, month);
  if (!issue) return null;
  const opened = await readIssue(issue, profile, username, month);
  if (opened.data?.deleted) return null;
  return { ...opened, issue };
}

// Delete a month that isn't closed. It's saved as an encrypted "deleted" copy, like any
// save, so only its engineer can do it and an older copy can't bring it back; the journal
// and workbooks then leave it out. `issueNumber` (remembered when it was saved) finds it
// even before GitHub's search has caught up. Returns false if it was never saved.
export async function deleteSaved(bundle, profile, username, month, issueNumber) {
  let issue = await findIssue(bundle, profile, month);
  if (!issue && issueNumber) {
    const candidate = await github(bundle, 'GET', `/repos/${bundle.dataRepo}/issues/${issueNumber}`).catch(() => null);
    if (isOurs(candidate, issueTitle(profile, month))) {
      store.set(issueCacheKey(bundle, profile, month), candidate.number); // so the save below replaces it
      issue = candidate;
    }
  }
  if (!issue) return false;
  await saveToGitHub(bundle, profile, username, {
    v: 1, employee: profile.name, month, savedAt: new Date().toISOString(), deleted: true, jobs: [], other: [],
  });
  return true;
}

export async function saveToGitHub(bundle, profile, username, data) {
  const index = profile.dataKeys.length - 1;
  const token = await seal(b64ToBytes(profile.dataKeys[index]), utf8.encode(JSON.stringify(data)), `etjm-ts|${username}|${data.month}`);
  const body = [MARKER, `Encrypted timesheet for ${monthInfo(data.month).label}. Only its engineer and the timewriting system can read it.`,
    '', '```etjm', `ETJM2.${index}.${token}`, '```', ''].join('\n');
  if (body.length > 65000) throw new AppError('too-big', 'This timesheet is too large to save. Remove some empty rows and try again.');
  const title = issueTitle(profile, data.month);
  const existing = await findIssue(bundle, profile, data.month);
  const issue = existing
    ? await github(bundle, 'PATCH', `/repos/${bundle.dataRepo}/issues/${existing.number}`, { title, body })
    : await github(bundle, 'POST', `/repos/${bundle.dataRepo}/issues`, { title, body });
  store.set(issueCacheKey(bundle, profile, data.month), issue.number);
  return issue;
}
