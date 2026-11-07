"""Presentation-only summaries; never use message matching to change session state."""
from textwrap import fill

from remoteops.core.models import SessionState


def error_summary(detail: str, state: SessionState | None = None) -> str:
    text = detail.casefold()
    if "outcome unconfirmed" in text or "side effects may" in text:
        message = "Outcome unknown. Inspect remote state before retrying."
    elif "method preparation failed; action was not sent" in text:
        message = "WMI method preparation failed. No action sent."
    elif "no new connection was authorized" in text:
        message = "No connection. Connect the host first."
    elif any(word in text for word in ("access denied", "access is denied", "0x80041003", "0x80070005")):
        message = "Access denied."
    elif any(word in text for word in ("authentication", "logon failure", "0x8007052e")):
        message = "Authentication failed. Check the account, domain, and credential."
    elif any(word in text for word in ("invalid class", "invalid namespace", "0x80041010", "0x8004100e")):
        message = "WMI class or namespace not found."
    elif "service was not found" in text:
        message = "No unique service match. Use the service name, not the display name."
    elif "dependent services may be running" in text:
        message = "Dependent services may block the stop request."
    elif any(word in text for word in ("startservice failed", "stopservice failed", "cannot restart service")):
        message = "Service action failed. Check the current state."
    elif "event log changed during retrieval" in text:
        message = "Event log changed during read. Rerun the query."
    elif any(word in text for word in ("invalid query", "0x80041017")):
        message = "Invalid query. Check class, properties, and filters."
    elif any(word in text for word in ("row/size limit", "size limit", "rows;", "row limit", "row cap", "exceeded 10000")):
        message = "Result too large. Narrow filters or reduce the row count."
    elif any(word in text for word in ("timed out", "time budget", "deadline", "timeout")):
        message = "Operation timed out. Check the host or reduce the request."
    elif any(word in text for word in ("unexpected cim", "trailing wmi", "projected property", "could not be read")):
        message = "WMI response parse failed."
    elif any(word in text for word in ("connection refused", "unreachable", "rpc server", "0x800706ba")):
        message = "Host unreachable. Check the address and network access."
    elif "unknown command" in text:
        message = "Unknown command."
    elif "python command cancelled" in text:
        message = "Command cancelled. Completed changes remain."
    elif "python command failed" in text:
        message = "Plugin failed. See traceback below."
    else:
        message = "Failed. See diagnostic below."
    message = fill(message, width=80)
    if state is SessionState.CONNECTED:
        message += "\nSession connected."
    elif state is SessionState.ERROR:
        message += "\nConnection lost. Reconnect."
    return message
