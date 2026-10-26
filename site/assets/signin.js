// Sign-in page: each engineer has a username and password from the office.
// The password unlocks the encrypted job list in this browser.

import {
  $, signIn, saveSession, loadSession, clearSession, lastName, normalizeUsername, homeFor, SIGN_IN_AGAIN, drafts, toast, holdToShow,
} from './app.js';

const params = new URLSearchParams(location.search);
const next = params.get('next') && /^[a-z]+\.html(\?[\w=&.-]*)?$/.test(params.get('next')) ? params.get('next') : null;

if (params.has('signout')) {
  // Signing out also removes this person's already-saved timesheet copies from this computer.
  const leaving = loadSession();
  clearSession();
  const kept = leaving ? drafts.forgetSaved(leaving.username) : 0;
  if (kept) setTimeout(() => toast(`${kept} month${kept === 1 ? ' has' : 's have'} unsaved changes, so ${kept === 1 ? 'it was' : 'they were'} kept on this computer. Sign in and Save to send ${kept === 1 ? 'it' : 'them'}.`, { duration: 9000 }), 0);
} else if (loadSession()?.uk) location.replace(next || 'home.html');

const form = $('#signin-form');
const userInput = $('#username');
const passwordInput = $('#password');
const message = $('#message');
const button = $('#submit');
userInput.value = lastName.get();
if (Object.hasOwn(SIGN_IN_AGAIN, params.get('why') || '')) showMessage(SIGN_IN_AGAIN[params.get('why')]);
(userInput.value ? passwordInput : userInput).focus();

// Holding the eye down shows what's been typed in the password box; letting go hides it.
const hidePassword = holdToShow($('#peek'), [passwordInput]);

// Going Back to this page restores it as it was left (button disabled, "Signing in…").
// Reset it so whoever is at the computer can sign in again, as themselves or someone else.
window.addEventListener('pageshow', (event) => {
  if (!event.persisted) return;
  button.disabled = false;
  button.textContent = 'Sign in';
  passwordInput.value = '';
  hidePassword();
  message.hidden = true;
});

function showMessage(text, field) {
  message.textContent = text;
  message.hidden = false;
  if (field) field.focus();
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  message.hidden = true;
  const username = normalizeUsername(userInput.value);
  if (!username) return showMessage('Enter your username.', userInput);
  if (!passwordInput.value.trim()) return showMessage('Enter your password.', passwordInput);
  button.disabled = true;
  button.textContent = 'Signing in…';
  try {
    const { session, profile } = await signIn(username, passwordInput.value);
    lastName.set(session.username);
    hidePassword();
    saveSession({ ...session, remember: $('#remember').checked });
    location.href = next || homeFor(profile);
  } catch (error) {
    button.disabled = false;
    button.textContent = 'Sign in';
    showMessage(error.message, error.code === 'bad-login' ? passwordInput : null);
    if (error.code === 'bad-login') passwordInput.select();
  }
});
