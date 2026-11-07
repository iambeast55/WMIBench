def run(ctx, args):
    return ctx.query(
        class_name="Win32_Service",
        properties={"Name": "string", "State": "string", "StartMode": "string"},
        filters=[{"property": "Name", "operator": "eq", "value": args["service_name"]}],
    )
