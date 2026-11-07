def run(ctx, args):
    ctx.progress("Reading BIOS inventory")
    rows = ctx.query(
        class_name="Win32_BIOS",
        properties={"Manufacturer": "string", "SMBIOSBIOSVersion": "string"},
    )
    return [{"Vendor": row.get("Manufacturer"), "Version": row.get("SMBIOSBIOSVersion")}
            for row in rows]
