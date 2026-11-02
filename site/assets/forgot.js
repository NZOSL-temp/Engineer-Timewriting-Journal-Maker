// "Forgot your password?": email the office's mailbox for a reset link (the Action reads it),
// or, on websites set up with the older reset helper, ask that. The page shows the same answer
// whether or not the username exists or has an email address.

import { $, lastName, normalizeUsername, requestReset, resetHelper, resetRequestEmail } from './app.js';

const form = $('#forgot-form');
const input = $('#username');
const button = $('#submit');
const message = $('#message');
input.value = lastName.get();
input.focus();

let how = null; // { email } or { url }, from the sign-in file
const finding = resetHelper();
finding.then((found) => {
  how = found;
  if (!how) {
    form.hidden = true;
    $('#off').hidden = false;
  } else if (how.email) {
    $('#forgot-intro').textContent = 'Enter your username. Your email app then opens a short email to the office asking for a link to '
      + 'choose a new password: send it, and the link comes back to the email address the office has for you.';
    button.textContent = 'Write the email';
  }
}).catch(() => { /* shown when they try */ });

// Open the request email to the office's mailbox in their email app.
function writeEmail(mailbox, username) {
  const mail = resetRequestEmail(mailbox, username);
  lastName.set(username);
  $('#mail-user').textContent = username;
  $('#mail-to').textContent = mailbox;
  $('#mail-subject').textContent = mail.subject;
  $('#mail-link').href = mail.href;
  form.hidden = true;
  $('#mail').hidden = false;
  location.href = mail.href;
}

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
  how = how || await finding.catch(() => null); // (typed before the page had found out)
  if (how?.email) return writeEmail(how.email, username);
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
