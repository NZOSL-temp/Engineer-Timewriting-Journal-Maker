# Reset helper (for "Forgot your password?")

The website runs on GitHub Pages, which can't send email, and someone who has forgotten their
password can't reach GitHub. `Code.gs` is a small Google Apps Script that does both for the
office:

- the sign-in page's **Forgot your password?** sends it a hash of the username, and it opens a
  "Password reset request" issue in the private data repo;
- the data repo's Action then asks it to email the reset link. These requests must be signed with
  `RELAY_SECRET`, so nobody else can use it to send email.

The emails come from the Google account that owns the script, so use an account for the office
(for example a Gmail address made for this), not someone's personal one. How it all fits together
is in [SECURITY.md](../SECURITY.md#forgotten-passwords).

## Setting it up

You need about 15 minutes, the office's Google account, and the nzosl-admin GitHub account.

### 1. Create the script

1. Signed in to the office's Google account, open <https://script.google.com> and click **New project**.
2. Click **Untitled project** (top left) and rename it `Engineer Timewriting reset helper`.
3. Open [Code.gs on GitHub](https://github.com/nzosl-admin/Engineer-Timewriting-Journal-Maker/blob/main/relay/Code.gs),
   click the **Copy raw file** button (two overlapping squares, top right of the code).
4. Back in the script, select everything in `Code.gs` (Cmd+A), paste (Cmd+V), and save (Cmd+S).

### 2. Give it a GitHub token

1. On GitHub, go to <https://github.com/settings/personal-access-tokens> and click **Generate new token**.
2. Token name `Timewriting reset requests`; Resource owner **nzosl-admin**; Expiration as long as allowed
   (put a reminder in your calendar); Repository access **Only select repositories** →
   **nzosl-admin/Engineer-Timewriting-Data**; Permissions → **Issues: Read and write** (nothing else).
3. Click **Generate token** and copy it.

### 3. Script properties

1. In the script, click **Project Settings** (the gear, left edge), scroll to **Script Properties**,
   and click **Add script property** three times:

   | Property | Value |
   | --- | --- |
   | `GITHUB_TOKEN` | the token you just copied |
   | `DATA_REPO` | `nzosl-admin/Engineer-Timewriting-Data` |
   | `SITE_URL` | `https://nzosl-admin.github.io/Engineer-Timewriting-Journal-Maker/` |

2. Click **Save script properties**.

### 4. Run setup once

1. Click **Editor** (`< >`, left edge). In the toolbar, choose `setup` in the function list and click **Run**.
2. Click **Review permissions**, choose the office account, then **Advanced** →
   **Go to Engineer Timewriting reset helper (unsafe)** → **Allow**. ("Unsafe" only means Google
   hasn't reviewed this private script.)
3. The **Execution log** shows `Script properties: all set`, how many sign-ins it found, and a
   line starting `RELAY_SECRET`. Copy the long value after the colon. A test email arrives in the
   office account's inbox.

### 5. Publish it

1. Click **Deploy** → **New deployment**. Click the gear next to *Select type* → **Web app**.
2. Description `Reset helper`; **Execute as: Me**; **Who has access: Anyone**. Click **Deploy**.
3. Copy the **Web app URL** (it ends in `/exec`).

### 6. Tell the data repo

On <https://github.com/nzosl-admin/Engineer-Timewriting-Data/settings/secrets/actions>:

1. **Secrets** tab → **New repository secret**: name `RELAY_SECRET`, value from step 4.
2. **Variables** tab → **New repository variable**: name `RELAY_URL`, value the Web app URL from step 5.
3. **Actions** → **Update website and journals** → **Run workflow**.

Then add everyone's email address on the website's **Admin** page (Engineers, and Office for admins).

## Later

- **Changing the code:** paste the new `Code.gs`, save, then **Deploy** → **Manage deployments** →
  pencil → **Version: New version** → **Deploy**. The URL stays the same.
- **The token expires:** make a new one the same way and replace `GITHUB_TOKEN` in Script Properties.
- **Turning it off:** delete the `RELAY_URL` variable. The sign-in page then says to ask the office.
