# Saved Credentials

## Manage Profiles

Open Credentials, select a profile, and use Edit (or double-click). You can change
its name, username, authentication method, domain settings, and saved secret. Editing keeps the
same profile ID and host assignments. Existing passwords are never displayed or
copied into the editor: leave Password blank to keep the saved value, or enter a
replacement. Save in database is enabled by default for new profiles. Changing between
password, NT hash, RC4 key, and AES key requires a replacement secret.

Uncheck Save in database to remove the stored secret and prompt at connection time.
New Kerberos profiles use an explicit domain and DC; existing profiles without a DC
retain their old system Kerberos behavior. See [Authentication](AUTHENTICATION.md).
Delete asks for
confirmation, removes the profile and its database secret, and clears its host
assignments. Disconnect hosts using a profile before editing or deleting it.

A saved password is automatically resolved on connection, including after app
restart and in the multi-host workflow. No repeated typing or master-password
prompt is needed. Passwords entered in one-off connection prompts are not saved;
use Credentials to opt into persistence. They never belong in command arguments.

## Storage And Backup

Profiles and encrypted secrets live in `wmibench.sqlite3`. Passwords, hashes, and keys use
authenticated Fernet encryption and are bound to the profile ID. Metadata and
password updates commit together; a failed encryption/database write does not
leave half an update. Plaintext passwords are not written into the database.

The encryption key is in `wmibench.key` beside the database:

- Linux: `~/.local/share/WMIBench/wmibench.key`, owned by the current user with
  permissions 0600. A key readable by other users is rejected.
- Windows: `%LOCALAPPDATA%/WMIBench/wmibench.key`, protected using the current
  Windows user's DPAPI. A copied key may not be usable from another account/machine.

Close the app before backing up the database and key together. Protect that backup
and never attach the key or credential database to a diagnostic report. Copying
only the database does not make saved passwords usable. A missing key is never
silently regenerated while encrypted secrets exist; restore the original key.
An unreadable credential produces an explicit error, not a plaintext fallback.

Automatic unlocking means this is not a defense against compromise of the logged-in
user or an administrator. On Linux, anyone who obtains both the readable key and
database can decrypt the passwords. Use full-disk encryption and restricted backups.
Host/profile metadata and command history are ordinary unencrypted SQLite data.
Deletion removes active database records, not a guarantee of forensic erasure from
backups or SQLite free pages.

## Existing Installations

The database schema updates automatically without resetting hosts/history. Older
keychain-backed profiles are copied to encrypted database storage on their first
successful lookup. If the keychain is unavailable, Edit the profile and save the
password once; subsequent connections use the database. Original keychain copies
are not removed automatically; manage those separately in the OS keychain if needed.
Profiles previously configured to prompt keep that behavior until edited.

Local tests cover encrypted persistence, profile changes, password replacement and
removal, transaction rollback, ciphertext tampering/profile swapping, missing keys,
and UI connection reuse without prompting. Windows DPAPI is tested locally; Linux
permission checks and live host compatibility still need your Linux lab pass.
