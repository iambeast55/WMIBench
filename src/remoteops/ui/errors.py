"""Presentation-only summaries; never use message matching to change session state."""
from textwrap import fill

from remoteops.core.models import SessionState


def error_summary(detail: str, state: SessionState | None = None) -> str:
    text = detail.casefold()
    if "outcome unconfirmed" in text or "side effects may" in text:
        message = "The action's final result is unknown. Check the remote state before retrying."
    elif "method preparation failed; action was not sent" in text:
        message = "Could not prepare the WMI method. No action was sent."
    elif "no new connection was authorized" in text:
        message = "Connection unavailable. Connect the host before running another command."
    elif any(word in text for word in ("access denied", "access is denied", "0x80041003", "0x80070005")):
        message = "This account lacks permission for the operation."
    elif any(word in text for word in ("authentication", "logon failure", "0x8007052e")):
        message = "Authentication failed. Check the account, domain, and saved credential."
    elif any(word in text for word in ("invalid class", "invalid namespace", "0x80041010", "0x8004100e")):
        message = "The host does not provide this WMI class or namespace."
    elif "service was not found" in text:
        message = "No unique matching service was found. Check the service name, not its display name."
    elif "dependent services may be running" in text:
        message = "Dependent services may be blocking the stop request."
    elif any(word in text for word in ("startservice failed", "stopservice failed", "cannot restart service")):
        message = "Windows could not complete the service action. Check its current state."
    elif "event log changed during retrieval" in text:
        message = "The event log changed while it was being read. Run the events query again."
    elif any(word in text for word in ("invalid query", "0x80041017")):
        message = "Invalid query. Check the class, properties, and filters."
    elif any(word in text for word in ("row/size limit", "size limit", "rows;", "row limit", "row cap", "exceeded 10000")):
        message = "Result too large. Narrow the filters or request fewer rows."
    elif any(word in text for word in ("timed out", "time budget", "deadline", "timeout")):
        message = "The operation took too long. Check the host or reduce the request."
    elif any(word in text for word in ("unexpected cim", "trailing wmi", "projected property", "could not be read")):
        message = "The WMI response could not be read."
    elif any(word in text for word in ("connection refused", "unreachable", "rpc server", "0x800706ba")):
        message = "The host could not be reached. Check its address and network access."
    elif "unknown command" in text:
        message = "Unknown command. Choose a command from the library."
    elif "python command cancelled" in text:
        message = "Command cancelled. Completed changes were not rolled back."
    elif "python command failed" in text:
        message = "Plugin failed. See the traceback below."
    else:
        message = "Failed. See the diagnostic below."
    message = fill(message, width=80)
    if state is SessionState.CONNECTED:
        message += "\nSession still connected."
    elif state is SessionState.ERROR:
        message += "\nConnection lost. Reconnect explicitly."
    return message
