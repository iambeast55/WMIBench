def run(ctx, args):
    path = ctx.object_path("Win32_Service", {"Name": args["service_name"]})
    result = ctx.invoke(path, args["action"], timeout_seconds=30)
    ctx.require_success(result, message=args["action"])
    return [{"Service": args["service_name"], "Method": args["action"], **result}]
