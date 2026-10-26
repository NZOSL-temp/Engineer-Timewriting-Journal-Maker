// "Forgot your password?": ask the office's reset helper to email a link. The page shows
// the same answer whether or not the username exists or has an email address.

import { $, lastName, normalizeUsername, requestReset, resetHelper } from './app.js';

const form = $('#forgot-form');
const input = $('#username');
const button = $('#submit');
const message = $('#message');
input.value = lastName.get();
input.focus();

resetHelper().then((url) => {
  if (url) return;
  form.hidden = true;
  $('#off').hidden = false;
}).catch(() => { /* shown when they try */ });

function show(text) {
  message.textContent = text;
  message.hidden = false;
  input.focus();
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  message.hidden = true;
  const username = normalizeUsername(input.value);
  if (!username) return show('Enter your username.');
  if (!/^[a-z0-9][a-z0-9._-]{0,39}$/.test(username)) return show('Usernames only use letters, numbers and dots, like alex.smith.');
  button.disabled = true;
  button.textContent = 'Sending…';
  try {
    await requestReset(username);
    lastName.set(username);
    $('#sent-user').textContent = username;
    form.hidden = true;
    $('#sent').hidden = false;
  } catch (error) {
    if (error.code === 'no-reset') {
      form.hidden = true;
      $('#off').hidden = false;
      return;
    }
    show(error.message);
  } finally {
    button.disabled = false;
    button.textContent = 'Email me a reset link';
  }
});
