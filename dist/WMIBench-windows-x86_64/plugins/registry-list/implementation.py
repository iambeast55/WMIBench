def run(ctx, args):
    result = ctx.invoke(
        "StdRegProv", "EnumKey",
        namespace="root/default",
        inputs={
            "hDefKey": ("uint32", 0x80000002),
            "sSubKeyName": ("string", args["subkey"]),
        },
        outputs={"ReturnValue": "uint32", "sNames": "string[]"},
    )
    ctx.require_success(result, message="EnumKey")
    return [{"Subkey": name} for name in result["sNames"] or []]
