/**
 * Reset helper for Engineer Timewriting: a Google Apps Script web app run by the office.
 *
 * The website is static (GitHub Pages), so it can't send email, and people who have
 * forgotten their password can't reach GitHub. This small script does both:
 *
 *  - "request": the sign-in page's "Forgot your password?" sends a hash of a username. If
 *    it's a real sign-in (checked against the website's public sign-in file), this opens a
 *    "Password reset request" issue in the private data repo. The data repo's Action makes a
 *    reset link and asks this script to email it. The answer is the same whatever happens,
 *    so it gives nothing away about who has a sign-in.
 *  - "send": the Action asks for an email to be sent. It has to be signed with RELAY_SECRET,
 *    recent, and not seen before, so nobody else can use this script to send email.
 *
 * Script properties (Project Settings, then Script properties):
 *   GITHUB_TOKEN  a fine-grained token with Issues: read and write, on the data repo only
 *   DATA_REPO     nzosl-admin/Engineer-Timewriting-Data
 *   SITE_URL      https://nzosl-admin.github.io/Engineer-Timewriting-Journal-Maker/
 *   RELAY_SECRET  made by running setup() once; copy it into the data repo's RELAY_SECRET secret
 *
 * See README.md next to this file for how to set it up.
 */

const PER_PERSON_MINUTES = 15; // at most one request per person this often
const PER_DAY = 20; // and at most this many a day altogether
const MAX_AGE_MS = 10 * 60 * 1000; // how old a signed "send" may be
const EMAIL_RE = /^[^@\s<>"',;:()[\]]+@[^@\s<>"',;:()[\]]+\.[^@\s<>"',;:()[\]]+$/;

function doPost(e) {
  let body = {};
  try {
    body = JSON.parse((e && e.postData && e.postData.contents) || '{}') || {};
  } catch (err) {
    body = {};
  }
  let answer;
  try {
    if (body.action === 'request') answer = request_(body);
    else if (body.action === 'send') answer = send_(body);
    else answer = { ok: false, error: 'unknown action' };
  } catch (err) {
    console.error(err);
    answer = body.action === 'request' ? { ok: true } : { ok: false, error: 'the helper had a problem' };
  }
  return json_(answer);
}

function doGet() {
  return json_({ ok: true, about: 'Engineer Timewriting reset helper' });
}

function json_(value) {
  return ContentService.createTextOutput(JSON.stringify(value)).setMimeType(ContentService.MimeType.JSON);
}

function props_() {
  return PropertiesService.getScriptProperties();
}

// ------------------------------------------------------------ reset requests

function request_(body) {
  const uid = String(body.uid || '');
  if (!/^[0-9a-f]{64}$/.test(uid)) return { ok: true };
  const cache = CacheService.getScriptCache();
  const lock = LockService.getScriptLock();
  lock.waitLock(10000);
  try {
    if (cache.get('person:' + uid)) return { ok: true };
    const day = Utilities.formatDate(new Date(), 'UTC', 'yyyy-MM-dd');
    let count = {};
    try {
      count = JSON.parse(props_().getProperty('COUNT') || '{}');
    } catch (err) {
      count = {};
    }
    const today = count.day === day ? Number(count.n) || 0 : 0;
    if (today >= PER_DAY) return { ok: true };
    if (!knownUsers_().includes(uid)) return { ok: true };
    cache.put('person:' + uid, '1', PER_PERSON_MINUTES * 60);
    props_().setProperty('COUNT', JSON.stringify({ day, n: today + 1 }));
  } finally {
    lock.releaseLock();
  }
  openIssue_(uid);
  return { ok: true };
}

// The hashed usernames in the website's public sign-in file (cached for 10 minutes).
function knownUsers_() {
  const cache = CacheService.getScriptCache();
  const cached = cache.get('users');
  if (cached !== null) return cached.split(',');
  const site = String(props_().getProperty('SITE_URL') || '').replace(/\/?$/, '/');
  const response = UrlFetchApp.fetch(site + 'data/users.json', { muteHttpExceptions: true });
  if (response.getResponseCode() !== 200) throw new Error('Could not read the sign-in file (' + response.getResponseCode() + ')');
  const ids = Object.keys(JSON.parse(response.getContentText()).users || {}).filter((id) => /^[0-9a-f]{64}$/.test(id));
  cache.put('users', ids.join(','), 600);
  return ids;
}

function openIssue_(uid) {
  const p = props_();
  const body = '<!-- etjm:reset-request:v1 -->\nA password-reset request from the sign-in page.\n\n```etjm\n' + uid + '\n```\n';
  const response = UrlFetchApp.fetch('https://api.github.com/repos/' + p.getProperty('DATA_REPO') + '/issues', {
    method: 'post',
    contentType: 'application/json',
    muteHttpExceptions: true,
    headers: {
      Authorization: 'Bearer ' + p.getProperty('GITHUB_TOKEN'),
      Accept: 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28',
    },
    payload: JSON.stringify({ title: 'Password reset request', body }),
  });
  if (response.getResponseCode() >= 300) console.error('GitHub refused the reset request: ' + response.getResponseCode());
}

// --------------------------------------------------------------- sending email

function send_(body) {
  const secret = props_().getProperty('RELAY_SECRET');
  if (!secret) return { ok: false, error: 'RELAY_SECRET is not set' };
  const fields = ['ts', 'to', 'subject', 'text', 'html'].map((k) => String(body[k] == null ? '' : body[k]));
  const expected = hex_(Utilities.computeHmacSha256Signature(fields.join('\n'), secret, Utilities.Charset.UTF_8));
  if (!same_(expected, String(body.sig || ''))) return { ok: false, error: 'bad signature' };
  if (!(Math.abs(Date.now() - Number(fields[0])) < MAX_AGE_MS)) return { ok: false, error: 'too old' };
  if (!EMAIL_RE.test(fields[1])) return { ok: false, error: 'not an email address' };
  const cache = CacheService.getScriptCache();
  if (cache.get('sig:' + expected)) return { ok: false, error: 'already sent' };
  cache.put('sig:' + expected, '1', 30 * 60);
  MailApp.sendEmail({ to: fields[1], subject: fields[2], body: fields[3], htmlBody: fields[4], name: 'Engineer Timewriting' });
  return { ok: true };
}

function hex_(bytes) {
  return bytes.map((b) => ((b + 256) % 256).toString(16).padStart(2, '0')).join('');
}

function same_(a, b) {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i += 1) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

// --------------------------------------------------------------------- setup

// Run this once from the editor (select setup, then Run). It makes RELAY_SECRET, checks the
// other settings, and sends you a test email so you know sending works.
function setup() {
  const p = props_();
  if (!p.getProperty('RELAY_SECRET')) {
    const bytes = Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256,
      Utilities.getUuid() + Utilities.getUuid() + Utilities.getUuid() + Date.now());
    p.setProperty('RELAY_SECRET', Utilities.base64EncodeWebSafe(bytes).replace(/=+$/, ''));
  }
  const missing = ['GITHUB_TOKEN', 'DATA_REPO', 'SITE_URL'].filter((k) => !p.getProperty(k));
  console.log(missing.length ? 'Still to add in Script properties: ' + missing.join(', ') : 'Script properties: all set.');
  if (p.getProperty('SITE_URL')) console.log('Sign-ins found on the website: ' + knownUsers_().length);
  const me = Session.getEffectiveUser().getEmail();
  MailApp.sendEmail({ to: me, subject: 'Engineer Timewriting reset helper works', body: 'This account can send the password-reset emails.' });
  console.log('Sent a test email to ' + me + '.');
  console.log('RELAY_SECRET (copy it into the data repo secret of the same name): ' + p.getProperty('RELAY_SECRET'));
}
