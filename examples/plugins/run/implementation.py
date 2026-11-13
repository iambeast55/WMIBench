import re
from remoteops.commands.plugin_api import PluginError


def run(ctx, args):
    path = args["absolute_path"]
    arguments = args.get("args", "").strip()
    ctx.progress("Starting process.")
    pattern = re.compile(r"^[a-zA-Z]:\\(?:[^\\/:*?\"<>|\r\n]+\\)*[^\\/:*?\"<>|\r\n]+\.(?:exe)$")
    command_line = f'"{path}"'
    if arguments:
        command_line += f" {arguments}"
    ctx.progress(command_line)
    if not pattern.match(path):
        raise PluginError(f"Invalid absolute path: {path}")
    result = ctx.invoke(
        "Win32_Process",
        "Create",
        inputs={"CommandLine": ("string", command_line)},
        outputs={
            "ReturnValue": "uint32",
            "ProcessId": "uint32",
        },
    )

    ctx.require_success(result, message="Start Process")
    return [{"Process Path": path, **result}]