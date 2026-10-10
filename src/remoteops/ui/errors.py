"""Presentation-only summaries; never use message matching to change session state."""
from textwrap import fill

from remoteops.core.models import SessionState


def error_summary(detail: str, state: SessionState | None = None) -> str:
    text = detail.casefold()
    if "outcome unconfirmed" in text or "side effects may" in text:
        message = "The action's final result is unknown. Check the remote state before trying it again."
    elif "no new connection was authorized" in text:
        message = "The existing connection is no longer available. Connect this host explicitly before another run."
    elif any(word in text for word in ("access denied", "access is denied", "0x80041003", "0x80070005")):
        message = "The account does not have permission for this operation. Check its access on the target."
    elif any(word in text for word in ("authentication", "logon failure", "0x8007052e")):
        message = "Authentication failed. Check the account, domain, and saved credential."
    elif any(word in text for word in ("invalid class", "invalid namespace", "0x80041010", "0x8004100e")):
        message = "This Windows host does not provide the requested WMI class or namespace."
    elif "service was not found" in text:
        message = "No unique matching service was found. Check the service name, not its display name."
    elif "dependent services may be running" in text:
        message = "Dependent services may prevent this service from stopping. Check dependencies and its current state."
    elif any(word in text for word in ("startservice failed", "stopservice failed", "cannot restart service")):
        message = "Windows could not complete the service action. Check the service's current state and the error details."
    elif "event log changed during retrieval" in text:
        message = "The event log changed while it was being read. Run the events query again."
    elif any(word in text for word in ("invalid query", "0x80041017")):
        message = "Windows rejected the query. Check its class, properties, and filters in the command definition."
    elif any(word in text for word in ("row/size limit", "size limit", "rows;", "row limit", "row cap", "exceeded 10000")):
        message = "The result is too large. Narrow the command's filters or request fewer records."
    elif any(word in text for word in ("timed out", "time budget", "deadline", "timeout")):
        message = "The operation took too long. Try a smaller request or check the target's responsiveness."
    elif any(word in text for word in ("unexpected cim", "trailing wmi", "projected property", "could not be read")):
        message = "The WMI response could not be read. Technical details identify the incompatible result."
    elif any(word in text for word in ("connection refused", "unreachable", "rpc server", "0x800706ba")):
        message = "The target could not be reached. Check its address, network, and remote-management access."
    elif "unknown command" in text:
        message = "Unknown command. Choose a command from the library."
    else:
        message = "The operation failed. Open the error details for the underlying message."
    message = fill(message, width=80)
    if state is SessionState.CONNECTED:
        message += "\nThe session is still connected; you can run another command."
    elif state is SessionState.ERROR:
        message += "\nThe connection is not usable. Reconnect explicitly before running another command."
    return message
