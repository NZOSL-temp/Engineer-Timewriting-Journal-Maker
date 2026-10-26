// Settings: your account, changing your password, and where password-reset emails go.
// A new password is turned into a key in this browser, and only that key, sealed with the
// person's own user key, goes to GitHub, so nobody else (not even the office) sees it.

import {
  $, el, toast, loadSession, clearSession, openSession, changePassword, checkPassword, passwordProblem,
  normalizePassword, holdToShow,
} from './app.js';

const session = loadSession();
if (!session?.uk) location.replace('index.html?next=settings.html');

function notice(tone, text) {
  $('#notices').replaceChildren(el('div', { class: `notice notice-${tone}` }, el('div', { class: 'notice-text' }, text)));
}

function showAccount(profile) {
  $('#user-name').textContent = profile.name;
  $('#timesheets-link').hidden = !profile.timesheets;
  $('#admin-link').hidden = !profile.admin;
  $('#acct-name').textContent = profile.name;
  $('#acct-username').textContent = profile.username;
  $('#acct-role').textContent = profile.admin && profile.timesheets ? 'Timesheets and the Admin page'
    : profile.admin ? 'The Admin page' : 'Timesheets';
  const where = profile.admin ? 'You can change it on the Admin page.' : 'To change it, ask the office.';
  $('#email-text').classList.toggle('muted', !profile.email);
  $('#email-text').replaceChildren(...(profile.email
    ? ['If you forget your password, use ', el('strong', {}, 'Forgot your password?'), ' on the sign-in page and a reset link is sent to ',
      el('strong', {}, profile.email), `. ${where}`]
    : [`The office hasn’t added an email address for you yet, so you can’t reset a forgotten password by email. ${profile.admin
      ? 'Add yours on the Admin page, under Office.' : 'Ask the office to add it.'}`]));
}

function setUpPasswordForm(bundle, profile) {
  const form = $('#pw-form');
  const current = $('#current-password');
  const fresh = $('#new-password');
  const again = $('#new-password-again');
  const hides = [holdToShow($('#peek-current'), [current]), holdToShow($('#peek-new'), [fresh]), holdToShow($('#peek-again'), [again])];
  const message = $('#pw-message');
  const done = $('#pw-done');
  const submit = $('#pw-submit');
  if (!bundle.saveToken) {
    form.replaceChildren(el('p', {}, 'Changing passwords needs saving to the office, which isn’t switched on yet.'));
    return;
  }
  const fail = (text, field) => {
    message.textContent = text;
    message.hidden = false;
    field?.focus();
  };
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (submit.disabled) return;
    hides.forEach((hide) => hide());
    message.hidden = true;
    done.hidden = true;
    if (!current.value.trim()) return fail('Enter your current password.', current);
    const problem = passwordProblem(fresh.value, profile.username);
    if (problem) return fail(problem, fresh);
    if (normalizePassword(fresh.value) !== normalizePassword(again.value)) return fail('The two new passwords don’t match.', again);
    if (normalizePassword(fresh.value) === normalizePassword(current.value)) return fail('That’s the same as your current password.', fresh);
    submit.disabled = true;
    submit.textContent = 'Checking…';
    try {
      if (!(await checkPassword(profile.username, current.value))) return fail('Your current password isn’t right.', current);
      submit.textContent = 'Changing… (about a minute)';
      const changed = await changePassword(bundle, profile, session, fresh.value);
      form.reset();
      done.textContent = changed ? 'Your password has been changed. Use the new one next time you sign in.'
        : 'This is taking longer than usual. In a few minutes, sign in with your new password; until it works, your old one still does.';
      done.hidden = false;
      toast(changed ? 'Password changed.' : 'Password change sent.', { tone: changed ? 'ok' : 'warn' });
    } catch (error) {
      fail(error.message);
    } finally {
      submit.disabled = false;
      submit.textContent = 'Change password';
    }
  });
}

async function start() {
  let opened;
  try {
    opened = await openSession(session);
  } catch (error) {
    if (error.code === 'signin') {
      clearSession();
      location.replace(`index.html?why=${error.reason || 'again'}&next=settings.html`);
      return;
    }
    notice('error', error.message);
    return;
  }
  showAccount(opened.profile);
  setUpPasswordForm(opened.bundle, opened.profile);
}

if (session?.uk) start();
