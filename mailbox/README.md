# Office mailbox (for "Forgot your password?")

**Forgot your password?** on the sign-in page opens a ready-made email from the person to the
office's Outlook.com mailbox, naming their username. The data repo's Action reads that mailbox
every half hour in New Zealand working hours (hourly otherwise; GitHub often starts these runs
late), and emails a link to choose a new password, from the same mailbox, to the address the
office has for that person on the Admin page. Nobody else gets the link, so asking for someone
else's reset achieves nothing. How it all fits together is in
[SECURITY.md](../SECURITY.md#forgotten-passwords).

The website reads and sends that mailbox's email through Microsoft Graph. For that, Microsoft
needs an *app registration*, which the office's Outlook.com account signs in to once.

You need about 20 minutes, the office's Outlook.com account (the address reset emails should
come from), and the nzosl-admin GitHub account.

## 1. A free Azure account (to register the app)

App registrations live in a Microsoft Entra directory, and a personal Outlook.com account gets
one with a free Azure account.

1. Open <https://azure.microsoft.com/free> and click **Try Azure for free** (or **Start free**).
2. Sign in with the **office's Outlook.com account**.
3. Follow the sign-up. Microsoft asks for a phone number, and a card to check you're a real
   person; the free account doesn't charge it unless you choose to upgrade.

## 2. Register the app

1. Open <https://entra.microsoft.com>, signed in as the office's account. In the left menu, open
   **App registrations** (under *Entra ID* or *Identity* › *Applications*) and click
   **New registration**.
2. **Name** `Engineer Timewriting mailbox`; **Supported account types** **Personal Microsoft
   accounts only**; leave **Redirect URI** empty. Click **Register**.
3. On the app's **Overview**, copy the **Application (client) ID** (it looks like
   `1a2b3c4d-1a2b-1a2b-1a2b-1a2b3c4d5e6f`). It isn't a secret.
4. Left menu **Authentication** › **Advanced settings** › **Allow public client flows**: **Yes**.
   Click **Save**.
5. Left menu **API permissions** › **Add a permission** › **Microsoft Graph** › **Delegated
   permissions**. Tick **Mail.Read**, **Mail.Send** and **offline_access** (**User.Read** is
   there already) and click **Add permissions**. No "admin consent" is needed: the office's
   account agrees to these itself when it signs in, next.

## 3. Connect it

1. On GitHub, open the data repo › **Actions** › **Connect the office mailbox** › **Run workflow**.
   Paste the client ID into the box and click **Run workflow**.
2. Click the new run (refresh if it isn't there yet), then the **mailbox** job, then the step
   **Sign in to the office mailbox**. It says something like: *To sign in, use a web browser to
   open the page https://microsoft.com/devicelogin and enter the code ABCD1234 to authenticate.*
3. Open that page, type the code, sign in with the **office's Outlook.com account**, and accept
   (it asks to let *Engineer Timewriting mailbox* read and send its email). The code works for
   15 minutes.
4. Back on GitHub the step says **Connected**, and the website is updated a minute later.
   **Forgot your password?** then offers to email the office.

## 4. Email addresses

On the website's **Admin** page, add each person's email address (under Engineers, and under
Office for admins) and **Save changes**. Reset links only ever go to these addresses.

## Later

- **If it stops working**, the **Update website and journals** runs warn that the office
  mailbox's sign-in has stopped working, and **Forgot your password?** says to ask the office.
  Run **Connect the office mailbox** again with the same client ID.
- **To turn it off**, run **Connect the office mailbox** with **Disconnect the mailbox instead**
  ticked. To take the app's access away on Microsoft's side too, open
  <https://account.live.com/consent/Manage> as the office's account.
- The requests arrive as ordinary emails in the office's inbox (or its junk folder), and the
  reset emails it sends are in its Sent Items. Delete them whenever you like. Only emails from
  the last three days that ask for an *Engineer Timewriting password reset* are read.
- The mailbox's sign-in is kept, encrypted, in the data repo's `mailbox.json`. Only the
  Actions can use it: it's sealed with a key from `MASTER_KEY` that admins' sign-ins don't
  carry. It's swapped for a fresh one every week.
- Websites set up with the older Google reset helper ([relay/](../relay/README.md)) keep using
  it until a mailbox is connected.
