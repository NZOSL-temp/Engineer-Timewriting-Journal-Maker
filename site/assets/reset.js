// The page a password-reset email links to. The link's secret is after the #, so it never
// reaches a web server; this page also takes it out of the address bar straight away (it's
// kept for this tab only, in case the page is reloaded). See openResetLink in app.js.

import {
  $, el, lastName, openResetLink, finishReset, passwordProblem, normalizePassword, holdToShow, clearSession, drafts,
} from './app.js';

const KEY = 'etjm.reset.v1';
// The email can arrive a little before the website has the link, so a new link waits for it.
const WAIT_MS = 2 * 60 * 1000;
const NEW_LINK_S = 5 * 60;

function readLink() {
  const fromHash = Object.fromEntries(new URLSearchParams(location.hash.slice(1)));
  if (fromHash.k) {
    try {
      sessionStorage.setItem(KEY, JSON.stringify(fromHash));
    } catch { /* this tab can't keep it; fine unless it's reloaded */ }
    history.replaceState(null, '', 'reset.html');
    return fromHash;
  }
  try {
    return JSON.parse(sessionStorage.getItem(KEY) || 'null');
  } catch {
    return null;
  }
}

function forget() {
  try {
    sessionStorage.removeItem(KEY);
  } catch { /* nothing kept */ }
}

const link = readLink();

function status(title, text, actions = []) {
  $('#status').hidden = false;
  $('#reset-form').hidden = true;
  $('#status-title').textContent = title;
  $('#status-text').textContent = text;
  $('#status-actions').replaceChildren(...actions);
}

const toForgot = () => el('a', { class: 'btn btn-primary btn-block', href: 'forgot.html' }, 'Email me a new link');
const toSignIn = (label = 'Back to sign in') => el('a', { class: 'btn btn-block', href: 'index.html' }, label);

async function open() {
  const started = Date.now();
  for (;;) {
    let opened;
    try {
      opened = await openResetLink(link);
    } catch (error) {
      status('Couldn’t check your link', error.message, [el('button', { class: 'btn btn-primary btn-block', type: 'button', onclick: open }, 'Try again')]);
      return;
    }
    const isNew = !(Number(link.t) > 0) || Date.now() / 1000 - Number(link.t) < NEW_LINK_S;
    const waiting = opened.status === 'updating' || (opened.status === 'missing' && isNew);
    if (!waiting || Date.now() - started > WAIT_MS) return show(opened);
    status('Getting your reset ready…', 'The website is still being updated with your link. This can take a minute after the email arrives.');
    await new Promise((resolve) => { setTimeout(resolve, 5000); });
  }
}

let current = null; // what the link opened, once it's checked
const fresh = $('#new-password');
const again = $('#new-password-again');
const hideNew = holdToShow($('#peek-new'), [fresh]);
const hideAgain = holdToShow($('#peek-again'), [again]);
const message = $('#message');
const button = $('#submit');

function fail(text, field) {
  message.textContent = text;
  message.hidden = false;
  field?.focus();
}

$('#reset-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (!current || button.disabled) return;
  message.hidden = true;
  hideNew();
  hideAgain();
  const problem = passwordProblem(fresh.value, current.username);
  if (problem) return fail(problem, fresh);
  if (normalizePassword(fresh.value) !== normalizePassword(again.value)) return fail('The two passwords don’t match.', again);
  button.disabled = true;
  button.textContent = 'Setting your password… (about a minute)';
  try {
    const done = await finishReset(current, fresh.value);
    forget();
    // Anyone still signed in on this computer as them is signed out.
    clearSession();
    drafts.forgetSaved(current.username);
    status(done ? 'Your password has been changed' : 'Nearly done',
      done ? 'Sign in with your new password. Anyone who had your old one can’t use it any more.'
        : 'This is taking longer than usual. Try signing in with your new password in a few minutes.',
      [el('a', { class: 'btn btn-primary btn-block', href: 'index.html' }, 'Sign in')]);
  } catch (error) {
    button.disabled = false;
    button.textContent = 'Set my new password';
    fail(error.message);
  }
});

function show(opened) {
  if (opened.username) lastName.set(opened.username);
  if (opened.status === 'replaced') {
    forget();
    return status('A newer link was sent', 'Only the most recent reset email works. Use the link in that one, or ask for another.', [toForgot(), toSignIn()]);
  }
  if (opened.status === 'expired' || opened.status === 'missing') {
    forget();
    return status('This link has run out', 'Reset links work once, for an hour. Ask for a new one, or sign in if you’ve remembered your password.', [toForgot(), toSignIn()]);
  }
  if (opened.status !== 'ok') {
    return status('This link isn’t complete', 'Open the link straight from the email, or copy all of it into your browser. If it still doesn’t work, ask for a new one.', [toForgot(), toSignIn()]);
  }

  current = opened;
  $('#status').hidden = true;
  $('#reset-form').hidden = false;
  $('#reset-user').textContent = opened.username;
  $('#reset-expires').textContent = new Date(opened.expires).toLocaleTimeString('en-NZ', { hour: 'numeric', minute: '2-digit' });
  fresh.focus();
}

if (!link?.k) {
  status('Choose a new password', 'This page needs the link from your password-reset email. Open that email and click the link, or ask for a new one.', [toForgot(), toSignIn()]);
} else {
  open();
}
