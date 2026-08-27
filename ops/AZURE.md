# The Azure app registration

**Nothing works until this is done, and one step of it needs somebody with tenant
administrator rights.** Do this first, and do the consent step before you try to run
anything: without it every call comes back "Insufficient privileges", which is not a bug in
the service and cannot be fixed from the service's side.

This gets the service a credential of its own. It does **not** sign in as James, and it does
not need his password. It is an *application* identity with permission to read and write
files in one drive.

---

## 1. Create the app registration

Azure portal → **Microsoft Entra ID** → **App registrations** → **New registration**.

- **Name:** `kbc-transcriber` (anything; it is only a label)
- **Supported account types:** *Accounts in this organizational directory only* — single
  tenant. This service has no business being multi-tenant.
- **Redirect URI:** leave empty. There is no interactive sign-in and no browser involved.

Register it, then copy two values off the Overview page:

| On the page | Goes in |
| --- | --- |
| **Directory (tenant) ID** | `GRAPH_TENANT_ID` |
| **Application (client) ID** | `GRAPH_CLIENT_ID` |

---

## 2. Create the client secret — and write down when it expires

**Certificates & secrets** → **Client secrets** → **New client secret**.

- **Description:** `transcriber service`
- **Expires:** pick the longest your tenant policy allows (24 months is the usual maximum).

**Copy the Value immediately.** Azure shows it exactly once; after you leave the page it is
gone and you have to make a new one. It goes in `GRAPH_CLIENT_SECRET`.

> ### Write the expiry date into the configuration.
>
> ```
> GRAPH_SECRET_EXPIRES_ON=2028-08-27
> ```
>
> This is the single most likely way this service dies. Without the date it runs perfectly
> for two years and then stops dead on a Tuesday morning with no prior warning of any kind —
> the same shape of silent failure the service exists to remove, just moved from the
> recordings to the credential. With the date, the morning email starts counting down 45
> days out, puts it in the subject line 14 days out, and tells the external monitor the
> morning is not fine. **Put a calendar reminder in as well.** Belt and braces.

---

## 3. The permissions — exactly these, and no more

**API permissions** → **Add a permission** → **Microsoft Graph** → **Application
permissions** (*not* Delegated — there is no signed-in user).

| Permission | Why it is needed | Could we use less? |
| --- | --- | --- |
| `Files.ReadWrite.All` | Read the recordings folder's change feed, download each recording, write the three `.md` files, and move an aged original into the archive folder. | Not really. `Files.Read.All` cannot write the outputs; there is no per-folder application scope on OneDrive. See the note below. |
| `User.Read.All` | Resolve `GRAPH_USER_ID` to the drive that holds the recordings. | Yes — you can drop it if you configure the drive id directly rather than the user. |

Then **Grant admin consent for \<tenant\>** — the button at the top of the API permissions
page.

> ### Admin consent is required and it is not optional.
>
> Application permissions on Microsoft Graph **always** require a tenant administrator to
> approve them; the service can never consent on its own behalf. If the button is greyed out
> for you, you are not an administrator and you need somebody who is. Until the Status
> column shows a green **"Granted for \<tenant\>"** against both permissions, every Graph
> call will fail with `403 Insufficient privileges to complete the operation`, and the
> service will stop with a message saying the credentials were refused. That is correct
> behaviour — it is not a configuration you can work around.

**On narrowing `Files.ReadWrite.All`.** It is broad: it grants the app read/write to *all*
files in the tenant. If that is more than you want to hand out, the supported way to fence it
is a **SharePoint / OneDrive application access policy** (`Set-SPOApplicationAccessPolicy`,
or the Graph `sites.selected` model for SharePoint sites), which restricts this specific app
registration to one user's OneDrive. It is worth doing, it needs the SharePoint admin
module, and the service behaves identically either way. Whether you do it or not, this
credential can do nothing but touch files — it cannot read mail, cannot read the directory
beyond a user lookup, and has no interactive session.

---

## 4. Find the three folder ids

The service works in driveItem ids, not paths — a path breaks the moment somebody renames a
folder, and an id survives a move.

With the credential from steps 1–3, or in **Graph Explorer** signed in as the drive's owner:

```
GET https://graph.microsoft.com/v1.0/users/{GRAPH_USER_ID}/drive/root:/CALLS
GET https://graph.microsoft.com/v1.0/users/{GRAPH_USER_ID}/drive/root:/CALLS-TRANSCRIPTS
GET https://graph.microsoft.com/v1.0/users/{GRAPH_USER_ID}/drive/root:/CALLS-ARCHIVE
```

Take the `"id"` from each answer:

| Folder | Variable |
| --- | --- |
| the recordings folder | `SOURCE_FOLDER_ID` |
| where the `.md` files go | `OUTPUT_FOLDER_ID` |
| where aged recordings are moved | `ARCHIVE_FOLDER_ID` |
| optional: where a half-written set of outputs is moved aside | `ORPHAN_FOLDER_ID` |

**They must be different folders.** The service refuses to start if any two of them are the
same, and that check exists for a reason: point the output folder at the recordings folder
and the live poll would classify every file it wrote as its own, skip it, and record nothing
— with no error, and a morning email blaming the phone.

**Do not nest the output folder or the archive folder inside the recordings folder.** The
change feed walks a folder's whole subtree.

---

## 5. The other credentials

- **The transcription key** (`OPENAI_API_KEY` or `ELEVENLABS_API_KEY` or
  `AZURE_SPEECH_KEY`) — whichever engine you chose. If it has an expiry, put it in
  `ENGINE_KEY_EXPIRES_ON`.
- **The analysis key** (`ANALYSIS_API_KEY`) — may be the same key as the OpenAI one; if
  `OPENAI_API_KEY` is set and no `ANALYSIS_API_KEY` is given, it is used. Expiry goes in
  `ANALYSIS_KEY_EXPIRES_ON`.
- **The monitor URL** (`HEARTBEAT_URL`) — create a check at healthchecks.io (or equivalent)
  with a period of one day and a grace of a few hours, and use its ping URL. **Treat it as a
  password**: anybody who has it can silence the alarm.
- **Downstream**, the Power Automate flow that carries the `.md` files into the record holds
  a fine-grained GitHub token scoped to that one repository with *Contents: read and write*.
  That is not this service's credential, but it expires too, and when it does the transcripts
  stop arriving in the record while this service keeps reporting perfect mornings. Put its
  expiry in the same calendar reminder.

---

## 6. Renewing the secret before it expires

1. Azure portal → the app registration → **Certificates & secrets** → **New client secret**.
   *Add the new one before deleting the old one* — both work at the same time.
2. Copy the Value.
3. Update `GRAPH_CLIENT_SECRET` **and** `GRAPH_SECRET_EXPIRES_ON` in the service
   environment.
4. Restart the service (`systemctl restart transcriber`, or redeploy the container).
5. `transcriber status` — the last successful poll should update within two minutes.
6. Delete the old secret in the portal.

Nothing is lost while the secret is expired: recordings pile up in OneDrive, the ledger's
place in the change feed does not move, and everything is collected once the credential
works again. It is an outage, not a loss.
