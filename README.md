# Engineer Timewriting Journal Maker

A calendar-style timesheet website for engineers that replaces the
"Engineer timewriting" spreadsheet. Engineers sign in with their own username and password,
pick jobs from the Business Central job list, enter their hours day by day and press **Save**.
They can come back to change a month, or reuse a month's rows for the next one. An **Admin** page lets the
office upload a new Business Central job report and download the journals. GitHub Actions build:

- **Timewriting Journal - Sep 2026.csv / .xlsx**: the journal uploaded to Business Central, laid out like the old Journal sheet, and
- **Engineer timewriting - &lt;name&gt; - September 2026.xlsx**: each engineer's month in the old "Timewriting 1-For Engineer" layout.

Website: https://nzosl-temp.github.io/Engineer-Timewriting-Journal-Maker/

## How it fits together

```
 Private data repo (every file encrypted)         Public website repo (this one)
 ────────────────────────────────────────         ──────────────────────────────
 input/report.json (the BC report) ──Action──▶ encrypted job list ──▶ site/data/bundle.enc.json
                                                                       │ GitHub Pages
 Timesheet issues (one per engineer per month) ◀──── Save ────── engineer's browser
        │
        └──Action──▶ output/<tag>.json: the month's journal (.csv + .xlsx) and engineer workbooks,
                     downloaded on the Admin page
```

- **This repo is public.** It holds only the website and the tools, never real data. `.gitignore` blocks spreadsheets and CSVs.
- **Everything private is encrypted**, with keys that come from one secret (`MASTER_KEY`) kept only in GitHub Actions. That includes every file in the private data repo, so even a copy of that repo can't be read without an admin's password or `MASTER_KEY`. Each person has their own keys, unlocked by their password in the browser. The job list is readable by anyone signed in. Each timesheet is end-to-end encrypted for its engineer. Engineers can change their password to one the office never sees. See [SECURITY.md](SECURITY.md) for the full design and what it does and doesn't protect against.
- **Saving** writes the month to an issue in the private data repo, encrypted, using a fine-grained token (Issues only, that repo only) so engineers don't need GitHub accounts. While typing, hours are also kept in the browser, so nothing is lost before Save.
- **The journal** has one line per job and CAPEX/REVEX with hours (hours × the engineer's recharge rate), then a balancing line per engineer to GL 9011. Rates live only in the data repo's encrypted `settings.json`; each engineer's profile carries just their own.

Working with the data repo (updating jobs, rates, downloading journals) is described in [`data-repo-template/README.md`](data-repo-template/README.md).

## Using the website

1. Sign in with your username and password from the office. **My timesheets** lists your months. Open one to change it, or **Delete** one the office hasn't closed (it's taken out of the journal too). **New timesheet** starts another month, empty or with **Copy job template from** an earlier month (the latest, unless you pick another): its jobs and other-time rows, without the hours. Jobs that are closed in Business Central are left out unless **Also copy closed jobs** is ticked. Hold the eye next to the password box to see what you've typed. Forgot it? **Forgot your password?** emails you a link to choose a new one (if the office has your email address).
2. Type a job number (`9683`, `J9683` or `JOB009683`) in **Add a job**. Location, owner and description fill in from the job list and can be edited. The job gets a CAPEX line, a REVEX line or both, depending on which tasks are open in Business Central.
3. Type hours into the days. The search box next to **Jobs** shows only the jobs that match what you type (a number like 9683 or J9683-DU-80, a location, owner or words from the description), to find one quickly. Blank means 0. `7.5`, `7,5` and `7:30` all work. Arrow keys and Enter move between cells, and you can paste a block straight from Excel.
4. Add leave, general time and other non-job rows under **Other time**. These are recorded in your engineer workbook, not the journal.
5. Press **Save** (or Ctrl+S). Jobs with no hours are kept on your timesheet and in your workbook, but only hours go into the journal.
6. **Settings** (top right) shows your account, lets you change your password to one only you know, and shows where password-reset emails go.

**Admins** (listed under `admins` in the data repo's settings) sign in to the **Admin** page, where they can:
- upload a new *Job Task Overview* report, which is checked, saved in the private repo and published to the website within a minute or two;
- add and remove engineers, set each engineer's recharge rate (there's no default rate) and email address (for password resets), and see, copy or reset passwords;
- view and download each month's journal and engineer workbooks, or tick months and use **Download all journal CSVs** and **Download all engineer workbooks** (several months come as one .zip);
- **Close** a month once its journal is uploaded, which freezes it (with fireworks, as for an engineer's Save);
- **View** an engineer's pages as they see them, read-only (from their saved months; nothing can be changed or saved).

Engineers can copy their jobs and other-time rows from any month they've saved (**New timesheet** on My timesheets). Closed months are read-only and can't be deleted. In the Admin page's **View**, the office can also see each engineer's **Journal preview**.

## Repository layout

| Path | What |
| --- | --- |
| `site/` | The website (plain HTML, CSS and JavaScript modules, no build step). Deployed by `.github/workflows/pages.yml`. |
| `tools/` | Python used by the data repo's Actions: reading the BC report, encrypting the job list, building the journal and workbooks. `etjm_vault.py` lets someone with `MASTER_KEY` look at or change the data repo's encrypted files by hand. |
| `tests/` | Unit tests (`python -m unittest discover -s tests`) using made-up data. |
| `data-repo-template/` | Workflows, settings and README for the private data repo. |
| `relay/` | The reset helper: a Google Apps Script the office runs, which takes "Forgot your password?" requests and sends the reset emails. See [relay/README.md](relay/README.md). |

## Setting it up from scratch

1. Make a `MASTER_KEY`: 32 random bytes, base64 (`python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"`). Keep it in a password manager.
2. Create a private data repo from `data-repo-template/`. With `MASTER_KEY` set in your shell, encrypt the settings and the BC report into it, so nothing is ever committed in the clear:
   `python tools/etjm_vault.py --data-dir DATA import my-settings.json settings.json` and
   `python tools/etjm_vault.py --data-dir DATA import "Job Task Overview - BC report.xlsx" input/report.json`
   (start `my-settings.json` from `settings.example.json`, and don't commit it).
3. Add `MASTER_KEY` to the data repo as a secret. Create an SSH key pair. Add the public half to **this** repo as a deploy key with write access, and the private half to the data repo as the `SITE_DEPLOY_KEY` secret.
4. Create a fine-grained personal access token (Repository access: only the data repo; Permissions: **Issues → Read and write**) and add it to the data repo as `SAVE_TOKEN`. Create a second one with **Contents → Read and write** and **Actions → Read**, for the Admin page, and add it as `ADMIN_TOKEN`.
5. In this repo, set **Settings → Pages → Source** to *GitHub Actions*.
6. Run **Update website and journals** in the data repo.
7. Optional, for **Forgot your password?**: set up the reset helper as in [relay/README.md](relay/README.md), and add email addresses on the Admin page.

## Local development

```sh
python -m pip install --require-hashes --no-deps -r tools/requirements.txt
python -m unittest discover -s tests
python -m http.server -d site 8000   # needs site/data/bundle.enc.json and users.json to sign in
```
