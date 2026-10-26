# Security

This site runs on GitHub Pages, which can only serve files: there is no server to check
passwords or hold secrets. So everything private is **encrypted**, and the keys are
arranged so that each person can open only what they're meant to. The one place secrets
are handled in the clear is GitHub Actions in the private data repo.

## What's protected, and how

| What | Where it lives | Who can read it |
| --- | --- | --- |
| Business Central report (costs, consultants, people) | private data repo, `input/report.json`, encrypted | admins, never published |
| Job list (number, description, location, owner, CAPEX/REVEX) | website, `site/data/bundle.enc.json`, encrypted | anyone signed in |
| An engineer's timesheets | GitHub issues in the private data repo, **end-to-end encrypted** | that engineer, and the Actions that build the journal |
| Recharge rates, who works here, closed months | private `settings.json`, encrypted; each engineer's own rate in their encrypted profile | admins; each engineer sees only their own rate |
| Passwords the office issues | private `users.json`, encrypted twice | admins (to hand them out) |
| Passwords people choose themselves | **not stored anywhere** | nobody, not even admins |
| Email addresses (for password resets) | private `settings.json`, encrypted; each person's own in their encrypted profile | admins; each person sees only their own |
| Journals, engineer workbooks and the verified copy of each timesheet | private data repo, `output/` and `timesheets/`, encrypted, one file per month with a meaningless name | admins (Admin page) |

**Every file in the private data repo is encrypted** (AES-256-GCM with a key derived from
`MASTER_KEY`, each file bound to its path so files can't be swapped or altered unnoticed).
A copy of that repo — cloned, leaked, or read by someone who gets into a GitHub account
with access — shows only random-looking data: no names, rates, jobs, hours or even which
months exist. Opening it needs `MASTER_KEY` or an admin's password. Commit messages and the
Actions' logs name nobody either. The repos were started afresh when this was introduced,
so no earlier unencrypted version exists in their history.

## Keys

* **`MASTER_KEY`** (a GitHub Actions secret in the data repo) is the root of everything.
  It never leaves GitHub Actions. All the keys below come from it, or are stored
  encrypted under it.
* **File key:** derived from `MASTER_KEY`; encrypts every file in the data repo. Admins'
  profiles carry it, so the Admin page can read and write those files in the browser.
* **Job-list key:** derived from `MASTER_KEY` and an *epoch* number. The epoch goes up
  whenever someone is removed or reset, so a sign-in that's been taken away can't read
  the job list (or the save token inside it) any more.
* **User key** (random, one per person): opens that person's profile. Their password
  unlocks it in the browser (PBKDF2-SHA256 with 600,000 iterations, then AES-256-GCM).
  An admin **reset** replaces it, which signs the person out everywhere.
* **Timesheet keys** (random, one per person): encrypt their timesheets in the browser
  before they reach GitHub. The ciphertext is bound to the person and the month, so it
  can't be copied to someone else or to another month.
* **Reset-link secret** (random, one per emailed link): opens only a copy of a *new* user
  key made for that reset. See below.

Every encrypted item uses AES-256-GCM with associated data naming what it is and whose
it is. Issue titles carry a random-looking tag rather than a name.

## Forgotten passwords

**Forgot your password?** on the sign-in page sends a hash of the username to the office's
*reset helper*, a small Google Apps Script (`relay/Code.gs`) running on the office's Google
account. It checks the hash belongs to a real sign-in and opens a "Password reset request"
issue in the data repo. The page shows the same answer whatever happens, so it can't be used
to find out who has a sign-in or an email address.

The Action then makes a **new** user key and timesheet key for that person, and emails them
a link to `reset.html` through the helper. The link holds a random secret after the `#`, so
it never reaches a web server, and the page removes it from the address bar at once. The
secret opens only a copy of the new user key, published next to their sign-in, and a short
profile with just enough to send a new password in. When they set one, it's sealed with the
new key, so only someone with the link could have sent it. The new keys then replace the old
ones, and the epoch moves on, which signs out anyone who had the old password.

* The old password keeps working until the link is used, so asking for resets can't lock
  anyone out.
* A link works once, for an hour, and a newer link replaces it. The Action removes links
  that have run out, at the latest within six hours (it runs every six hours for that).
* At most one email per person every 15 minutes, and the helper takes at most 20 requests
  a day in all.
* The helper sends email only when asked by the Action, with a request signed with
  `RELAY_SECRET` (HMAC-SHA256), recent, and not seen before. So nobody else can use it to
  send email. The token it holds can only open issues in the data repo.
* As with any reset by email, someone who can read a person's email can reset their
  password. That's why only the office sets email addresses (on the Admin page), and each
  person can check theirs on their **Settings** page.

## Attacks considered

* **A curious or malicious engineer.** Everyone signed in shares the save token, so they
  can see and edit the timesheet issues. They can't read them: each is encrypted with its
  owner's key. They can't forge or alter them either. The Actions accept a timesheet only
  if it decrypts with the newest key of the person its title names. The journal is built
  from the newest *verified* copy, which only the Actions can write (`timesheets/`), so
  editing, closing or replaying old issues changes nothing. The website warns the owner if
  their saved copy has been tampered with.
* **Someone who has left.** Removing them deletes their keys and moves the epoch on. They
  can't sign in, read the job list or save timesheets. They may still remember the save
  token, which lets them create junk issues only. Replace `SAVE_TOKEN` if that's a concern.
* **A stolen password.** An admin **New password** reset replaces the user key, signs them
  out everywhere and retires their old timesheet key.
* **Guessing passwords offline.** `site/data/users.json` is public, so passwords must
  withstand guessing at leisure. Issued passwords are random: about 60 bits for engineers
  and 80 bits for admins. Chosen passwords must be at least 12 characters, and each guess
  costs 600,000 PBKDF2 rounds.
* **Malicious web content.** The pages load only their own scripts and styles, with a strict
  Content Security Policy including Trusted Types. No HTML is ever built from data. The
  pages refuse to run inside another site's frame.
* **Supply chain.** Every GitHub Action is pinned to an exact commit, and every Python
  package to an exact version with file hashes (`pip --require-hashes`). XML in uploaded
  spreadsheets is parsed with `defusedxml` protection.

## What this design can't do

* **Anyone signed in can read the job list**, and could keep a copy.
* **Some activity is visible to anyone with access to the data repo:** how many timesheet
  issues there are, the month in each one's title, and when files changed. Not what's in them.
* **The public sign-in file confirms usernames.** `site/data/users.json` lists a hash of
  each username, so someone who guesses `firstname.lastname` can check it's in use. Only a
  password opens anything.
* **Admins are trusted.** The admin token can read and change everything in the data repo.
  **View** on the Admin page shows an engineer's pages from the verified copies admins can
  already read; it can't save as them, because that needs the engineer's own timesheet key.
* **No server means no rate limiting or audit of sign-ins.** All GitHub activity made
  through the website shows as the owner of the tokens.
* **Tokens expire.** Fine-grained tokens have an end date: renew `SAVE_TOKEN` and
  `ADMIN_TOKEN` before then, or saving and the Admin page stop working.

Server-backed sign-in (for example Microsoft Entra ID, the same accounts as Business
Central) would remove the shared save token entirely. It needs a small backend, such as an
Azure Function, and IT to register an app.

## Recommended account settings

* Turn on **two-factor authentication** for every GitHub account that can reach these
  repos, and require it for the NZOSL-temp organisation (Settings → Authentication security).
* Keep `MASTER_KEY` in a password manager as well. GitHub can't show a secret again, and
  without it nothing in the data repo can be opened. To change it (for example if the only
  copy is GitHub's), add a `NEW_MASTER_KEY` secret: the next run re-encrypts everything
  with it and keeps everyone's password (see the data repo's README).
* Give tokens only the permissions listed in the data repo's README, for that one repo.

## Reporting a problem

Please tell the NZOSL office administrator directly rather than opening a public issue.
