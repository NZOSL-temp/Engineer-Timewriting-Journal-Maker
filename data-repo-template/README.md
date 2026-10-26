# Engineer Timewriting: data (private)

This private repo holds everything for the timewriting website that must not be public: the
Business Central report, the sign-ins, the saved timesheets, the recharge rates and the finished
journals. **Every file here is encrypted**, so the files on GitHub look like random text. Use the
website's **Admin page** to work with them. How it's all protected is in the website repo's
[SECURITY.md](https://github.com/nzosl-admin/Engineer-Timewriting-Journal-Maker/blob/main/SECURITY.md).

- Website: https://nzosl-admin.github.io/Engineer-Timewriting-Journal-Maker/
- Website code: https://github.com/nzosl-admin/Engineer-Timewriting-Journal-Maker

## Everyday tasks

| To… | Do this |
| --- | --- |
| Update the job list | On the website's **Admin** page, drop the **Job Task Overview** export from Business Central into the upload box. It's encrypted in the browser and saved here as `input/report.json`, and the page shows when the website has the new list (about a minute). Don't upload spreadsheets to GitHub directly: they'd be readable, and the Actions ignore them. |
| Get the journal for Business Central | On the **Admin** page, under **Journals**, download the month's **Journal CSV** (or **View** it first). To get several months at once, tick them and press **Download all journal CSVs** (or **Download all engineer workbooks**). |
| See one engineer's month | On the **Admin** page, under **Engineer workbooks**: laid out like the old "Timewriting 1-For Engineer" sheet. |
| Check for problems | **View** a month on the **Admin** page: it lists hours on jobs that aren't open, days over 24 hours and missing rates. |
| Let someone use the Admin page | Add them to `admins` in `settings.json` (see *Changing files by hand* below). They get a password like everyone else. Someone in both lists gets both pages. |
| Add an engineer | On the **Admin** page, under **Engineers**, type their name and recharge rate, press **Add engineer**, then **Save changes**. About a minute later their password is in the list (**Show** or **Copy** it to hand over). Their username is `firstname.lastname`. |
| Give someone a new password | On the **Admin** page, press **New password** next to them, then **Save changes**. This also signs them out everywhere, so use it if a password may have got out. Their new password appears in the list a minute later. |
| Let people choose their own password | Anyone can use **Settings** → **Change your password** on the website. The office never sees it; the Admin page then shows *Chosen by them*. |
| Let people reset a forgotten password | Add their email address on the **Admin** page (Engineers, or Office for admins), then **Save changes**. **Forgot your password?** on the sign-in page then emails them a link. It needs the reset helper (see *Secrets and variables* below). |
| Remove an engineer | On the **Admin** page, press **Remove** next to them, then **Save changes**. They can't sign in any more, but stay listed under *Former engineers* (and in `settings.json` with `"active": false`) so their past journals keep their rate. **Restore** brings them back. |
| Change a recharge rate | On the **Admin** page, change the number next to them, then **Save changes**. Every month that isn't closed is rebuilt with the new rate. |
| Close a month after uploading its journal | On the **Admin** page, under **Journals**, press **Close**. The month's files are frozen: engineers can't change it and rate changes don't affect it. **Reopen** undoes it. |
| Change other-time rows | Edit `otherTime` in `settings.json` (see *Changing files by hand* below). |
| Clear someone's month | They open the month, remove its rows and press **Save**. (Closing or editing timesheet issues on GitHub has no effect: the journal uses the last verified save.) |

Don't edit `output/`, `timesheets/` or `users.json` by hand. `timesheets/` holds the last verified copy of each timesheet, and the Actions rebuild the rest whenever someone saves.

## What's in here

| File | Holds |
| --- | --- |
| `settings.json` | Engineers, rates, admins, other-time rows, closed months |
| `users.json` | Sign-ins (their secrets are encrypted a second time inside) |
| `input/report.json` | The latest Business Central report |
| `generated/jobs.json` | The job list read from it |
| `timesheets/<tag>.json` | One month's verified timesheets |
| `output/<tag>.json` | One month's journal and engineer workbooks |

Each is `{"etjm": "encrypted", "v": 1, "data": "…"}`: AES-256-GCM with a key derived from `MASTER_KEY`, tied to the file's path. `<tag>` is a meaningless name, so the file names don't even show which months exist.

## Changing files by hand

Most changes are on the Admin page. For anything else (other-time rows, admins, journal templates), someone with `MASTER_KEY` uses the website repo's `tools/etjm_vault.py` on a clone of this repo:

```sh
export MASTER_KEY=…                       # from the password manager
python tools/etjm_vault.py --data-dir . list
python tools/etjm_vault.py --data-dir . show settings.json > /tmp/settings.json
# edit /tmp/settings.json, then:
python tools/etjm_vault.py --data-dir . import /tmp/settings.json settings.json
git commit -am "Update settings" && git push && rm /tmp/settings.json
```

`export output/<tag>.json some-folder` writes a month's journal and workbooks out as normal files. Keep plain copies off GitHub.

## How it works

1. One Action, **Update website and journals** (`.github/workflows/update.yml`), runs when `input/`, `settings.json` or `users.json` changes, after every save, password change or reset request on the website, and every six hours (to remove reset links that have run out). Its logs and commit messages name nobody.
2. **Sign-ins and job list.** It gives everyone in `settings.json` a sign-in in `users.json`; all its secrets are encrypted with keys derived from `MASTER_KEY`. It applies resets and password changes, and removes leavers. Then it publishes the encrypted sign-ins and the job list to the website. The job list keeps only jobs whose status is in `jobStatuses` (normally `Open`): job number, description, location, owner and CAPEX/REVEX. Costs, consultants, people and rates never leave this repo.
3. **Saving.** An engineer signs in, fills in the calendar and presses **Save**. Their browser encrypts the month with their own timesheet key and stores it as an issue here. The issue is titled `Timesheet 2026-09 · <random tag>`, so it names nobody, and only that engineer and the Action can read it.
4. **Journals.** The same Action opens each timesheet issue with its owner's key, and ignores anything that doesn't open or is older than the last verified copy in `timesheets/`. It then rewrites `output/`, encrypted:
   - one journal line per job and CAPEX/REVEX with hours. The hours are the sum of that row's days, and the amount is hours × recharge rate.
   - then one balancing line per engineer to GL account `balancingGlAccount`.
   - Date is the last day of the month. Description is `<name> Timewriting Sept 26`, and Document number is `EngtimewriteSept 26`.

## settings.json

| Setting | Meaning |
| --- | --- |
| `employees` | Engineers who fill in timesheets: `name` (as it should appear in the journal), `rechargeRate` (each engineer has their own; there's no default), optional `username` (default `firstname.lastname`), optional `email` (where password-reset links go), and `"active": false` for people who have left. They can't sign in, but their past journals keep their rate. |
| `closedMonths` | Months whose journals have been uploaded to Business Central, like `"2026-09"`. Their files are frozen: engineers can't change those months, and later rate changes don't affect them. |
| `admins` | People who can use the Admin page (upload the report, download journals): `name`, optional `username`, optional `email`. |
| `otherTime` | The non-job rows engineers can add, like General Time and the leave types. They go in the engineer workbooks, not the journal. |
| `jobStatuses` | Which job task statuses appear on the website. |
| `journal.balancingGlAccount` | GL account for each engineer's balancing line. |
| `journal.monthLabels` | Month names used in Description and Document number (`Sept`, …). |
| `journal.description`, `journal.documentNumber` | Templates. `{employee}`, `{monthLabel}`, `{yy}`, `{yyyy}`, `{mon}` and `{month}` are filled in. |
| `journal.dateFormat` | Date format in the CSV (`%d/%m/%Y` gives 30/09/2026). |
| `journal.csvUtf8Bom` | Start the CSV with a byte-order mark so Excel reads names with macrons correctly. |

## Secrets and variables (Settings → Secrets and variables → Actions)

| Secret | What it is | How to change it |
| --- | --- | --- |
| `MASTER_KEY` | 32 random bytes (base64): every other key comes from it, including the one that encrypts every file here. Keep a copy in a password manager. | Don't change it: every file here is encrypted with it. A new one means starting the repo again with fresh files, and everyone gets a new password. |
| `SAVE_TOKEN` | A fine-grained GitHub token with **Issues: read and write** on this repo only. The website uses it to save timesheets and send password changes. | Create a new token, update the secret, then run **Update website and journals** (Actions → Run workflow). Do this if someone leaves on bad terms. |
| `ADMIN_TOKEN` | A fine-grained GitHub token with **Contents: read and write** and **Actions: read** on this repo only. Only admins' sign-ins can unlock it. The Admin page uses it to upload reports, manage engineers and download journals. | Create a new token, update the secret, then run **Update website and journals**. |
| `SITE_DEPLOY_KEY` | Lets this repo update the encrypted job list in the website repo (deploy key with write access there). | Rarely needed. Generate a new key pair and replace both halves. |
| `RELAY_SECRET` | Shared with the reset helper (the Google Apps Script in the website repo's `relay/`), to sign the reset emails it sends. | Delete it from the script's properties and run `setup` there again, then paste the new one here. |
| `RELAY_URL` (a **variable**, not a secret) | The reset helper's web address, ending `/exec`. While it's set, the sign-in page offers **Forgot your password?** | Delete it to turn reset emails off. |
| `NEW_MASTER_KEY` (only while changing `MASTER_KEY`) | See *Changing MASTER_KEY* below. | Delete it once `MASTER_KEY` has the same value. |

## Changing MASTER_KEY

Do this if the key might have got out, or if nobody has a copy of it apart from GitHub (which can't show it).
Everyone keeps their password, and nobody notices.

1. Make a new key and copy it, without it ever being shown: in Terminal on a Mac, run
   `openssl rand -base64 32 | tr -d '\n' | pbcopy`.
2. Paste it into your password manager (as "Engineer Timewriting MASTER_KEY").
3. Here, add a secret named `NEW_MASTER_KEY` and paste the same key.
4. **Actions** → **Update website and journals** → **Run workflow**. Its first step re-encrypts
   everything with the new key, and renames the timesheet issues to match.
5. When it has finished, edit `MASTER_KEY` and paste the new key again, then delete `NEW_MASTER_KEY`.

If `MASTER_KEY` was replaced and nobody has the old value, the Action fails because nothing opens.
An admin's sign-in can still re-encrypt everything for the new key, keeping everyone's password:
see `tools/recover_master_key.py` in the website repo (run it on a computer with both repos).

Anyone who can sign in can, with some effort, get the save token. That lets them see the timesheet issues, but each one is encrypted for its owner. They can't read them, and changes they make are ignored. The save token can't reach anything else here. The admin token can, so only admins can unlock it — and even with it, the files here are encrypted.
