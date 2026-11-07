def run(ctx, args):
    if ctx.backend == "winrm":
        response = ctx.powershell(
            "$os = Get-CimInstance Win32_OperatingSystem; "
            "$bios = Get-CimInstance Win32_BIOS; "
            "[pscustomobject]@{Host=$os.CSName; OS=$os.Caption; Vendor=$bios.Manufacturer}"
        )
        return response["records"]

    ctx.progress("Reading operating system")
    systems = ctx.query(
        class_name="Win32_OperatingSystem",
        properties={"CSName": "string", "Caption": "string"},
    )
    ctx.check_cancelled()
    ctx.progress("Reading BIOS")
    bios = ctx.query(class_name="Win32_BIOS", properties={"Manufacturer": "string"})
    vendor = bios[0].get("Manufacturer") if bios else None
    return [{"Host": row.get("CSName"), "OS": row.get("Caption"), "Vendor": vendor}
            for row in systems]
