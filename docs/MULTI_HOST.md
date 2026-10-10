# Multi-Host Execution

Open **Multi-host** in the top toolbar. This first version runs read-only commands;
it does not provide bulk service changes or arbitrary PowerShell execution.

## Run A Batch

1. Check the saved hosts to include. Search matches names, addresses, groups, and
   tags. Select visible changes only the visible rows; filtering does not remove
   earlier selections. Check the selected count before running.
2. Choose an alias or enter an alias plus arguments, for example
   `events --log System --count 5` or `pending-reboot`. The Preflight column shows
   unsupported commands, busy hosts, missing profiles, and connection requirements.
3. Set Parallel hosts (1 to 12, default 4).
4. Connected hosts reuse their current sessions without a password prompt. To
   include disconnected hosts, enable Connect disconnected hosts. Run selected
   asks you to confirm the command, eligible/skipped counts, and new connections.
5. Enter any required passwords in the app prompt, never in command arguments.
   Each needed credential profile is resolved once on the UI thread. Cancelling
   a credential prompt skips new connections using that profile; other hosts continue.

The command definition is loaded and validated once before approval and is frozen
for the batch. Editing the library afterward cannot change queued work. Each host
uses its configured WMI/DCOM or WinRM backend; there is no transport fallback.

## Read-Only Eligibility

- The 20 inventory/diagnostic builtins qualify on their supported backends.
  `restart-service` does not qualify for bulk execution.
- A builtin PowerShell script must still exactly match the shipped script.
  Renaming a custom script to a builtin alias or setting `_modifies` to false
  does not grant eligibility. Arbitrary custom PowerShell scripts are excluded.
- Validated custom WMI SELECT definitions qualify on WMI hosts. A custom definition
  with a separate PowerShell implementation does not automatically qualify that
  PowerShell implementation for bulk use.
- This policy is computed by `command_read_only_modes` in the source, not by a
  user-controlled JSON `read_only` flag. New source-defined commands need review,
  backend support, and tests before joining the builtin set.

## Progress And Results

Host results shows Queued, Running, Succeeded, Failed, or Skipped independently.
Running means dispatched to the host worker, including any connection setup.
One host's command, connection, or local failure does not stop the remaining hosts.
Select an outcome for original output, technical errors, and structured records.
The summary supports sorting and filtering. Command duration is shown when available.

Combined results includes host identity, backend, outcome, duration, and errors.
Provider fields have a `Result.` prefix so they cannot replace the host identity.
Failed, skipped, and empty-result hosts still have an outcome row. The table displays
up to 10,000 rows to bound UI work; its count states when the display limit is reached.
The table's own export saves its visible rows. The footer's **Export all batch results
as CSV** icon exports the complete current batch, independent of table filters or
display limits. CSV cells beginning with spreadsheet formula markers are quoted as text.

Dispatched commands also appear in each host's Terminal, Table, and History.
Skipped hosts have no command-history entry because no command was attempted.
Batch summaries remain in the dialog until it closes or another run begins; use
the CSV export to retain the full batch summary, including skipped hosts.

## Cancellation And Connections

Cancel queued stops work not yet dispatched. Already-dispatched hosts finish
normally within their existing backend time limits; cancellation does not interrupt
RPC calls, close healthy sessions, or imply that active commands never ran.
Closing the dialog or pressing Escape while running requests queued cancellation
and keeps the dialog open until dispatched work finishes. Application shutdown also
waits for the batch to finish. A failed command is never automatically retried.

Hosts already in an Error state are skipped until explicitly reconnected in their
host tab. If a reused session becomes unavailable while queued, its command fails
without opening a replacement connection. Approving disconnected hosts does not
authorize reconnecting a failed session. Healthy connections remain available after
the batch, including new connections explicitly approved for that run.

## Suggested Lab Checks

1. Connect two WMI hosts and one WinRM host, then batch `sysinfo`. Check session
   reuse, one result per host, backend-specific fields, history, and CSV export.
2. Batch `svc AServiceThatDoesNotExist`, then `sysinfo`. Command errors must leave
   healthy sessions available for the second batch.
3. Include a disconnected host with the connection checkbox disabled: it is skipped.
   Enable the checkbox, decline confirmation, and verify that nothing runs. Approve
   a later run and check that only the required credentials are requested.
4. Run `bios` from the WMI query example across mixed backends. WinRM hosts are
   marked incompatible before dispatch; WMI hosts can run the query.
5. Set concurrency to 1, select several lab hosts, and cancel after the first starts.
   Queued hosts must be skipped and the dispatched host must finish without a retry.
6. Check that `restart-service` and edited builtin PowerShell scripts cannot run
   in a batch. Single-host workflows and their confirmations remain unchanged.

Local automated tests use synthetic sessions and offscreen Qt. Live host validation
is deferred to your lab; no SSH deployment or live remote action was performed.
