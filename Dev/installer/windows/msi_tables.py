"""Reads an MSI's database tables and unpacks its files, on Linux, with 7z
(p7zip) - enough of the format for extract_tcltk.py and
extract_vcruntime.py, which bundle files from python.org / Microsoft MSIs
into the Windows installer at build time.

An MSI is an OLE compound file: 7z pulls out its database streams (and any
embedded cab), and the File/Component/Directory tables say where each cab
entry (named by its File key) belongs.
"""

import os
import struct
import subprocess


def read_tables(streams_dir):
    def stream(name):
        with open(os.path.join(streams_dir, "!" + name), "rb") as f:
            return f.read()

    pool, data = stream("_StringPool"), stream("_StringData")
    long_refs = bool(struct.unpack("<I", pool[:4])[0] & 0x80000000)
    strings, offset, i = [None], 0, 4
    while i < len(pool):
        size, refs = struct.unpack("<HH", pool[i:i + 4])
        i += 4
        if size == 0 and refs != 0:
            size = struct.unpack("<I", pool[i:i + 4])[0]
            i += 4
        strings.append(data[offset:offset + size].decode("utf-8"))
        offset += size
    sref = 3 if long_refs else 2

    def column(buf, start, width, count):
        return [int.from_bytes(buf[start + k * width:start + (k + 1) * width], "little") for k in range(count)]

    # _Columns rows: Table (string), Number (i2), Name (string), Type (i2);
    # every table is stored column by column.
    cols = stream("_Columns")
    n = len(cols) // (2 * sref + 4)
    schema = {}
    for table, number, name, col_type in zip(
        column(cols, 0, sref, n),
        column(cols, n * sref, 2, n),
        column(cols, n * sref + n * 2, sref, n),
        column(cols, n * (2 * sref + 2), 2, n),
        strict=True,
    ):
        schema.setdefault(strings[table], []).append((number & 0x7FFF, strings[name], col_type))

    def table(name):
        spec = sorted(schema[name])
        buf = stream(name)
        widths = [sref if t & 0x0800 else (4 if (t & 0xFF) == 4 else 2) for _, _, t in spec]
        rows = len(buf) // sum(widths)
        values, pos = [], 0
        for (_, _, col_type), width in zip(spec, widths, strict=True):
            raw = column(buf, pos, width, rows)
            pos += width * rows
            values.append([strings[v] if v else None for v in raw] if col_type & 0x0800 else raw)
        return [dict(zip([s[1] for s in spec], row, strict=True)) for row in zip(*values, strict=True)]

    return table


def long_name(value):
    # "SHORTN~1|Long Name" -> "Long Name"
    return value.split("|")[-1]


def extract_msi(msi_path, work_dir, cab_path=None):
    """Unpacks msi_path under work_dir. The cab is the MSI's embedded one
    unless cab_path (an external cab shipped beside it) is given. Returns
    [(directory parts, file name, extracted path)], where directory parts
    are relative to TARGETDIR, e.g. (["DLLs"], "_tkinter.pyd", ...)."""
    streams_dir = os.path.join(work_dir, "streams")
    cab_dir = os.path.join(work_dir, "cab")
    subprocess.run(["7z", "x", "-y", f"-o{streams_dir}", msi_path], check=True, stdout=subprocess.DEVNULL)
    if cab_path is None:
        cabs = [name for name in os.listdir(streams_dir) if name.endswith(".cab")]
        if len(cabs) != 1:
            raise SystemExit(f"Expected one embedded cab in {msi_path}, found {cabs}")
        cab_path = os.path.join(streams_dir, cabs[0])
    subprocess.run(["7z", "x", "-y", f"-o{cab_dir}", cab_path], check=True, stdout=subprocess.DEVNULL)

    table = read_tables(streams_dir)
    directories = {row["Directory"]: row for row in table("Directory")}
    components = {row["Component"]: row for row in table("Component")}

    def dir_path(dir_id):
        parts = []
        while dir_id and dir_id != "TARGETDIR":
            row = directories[dir_id]
            name = long_name(row["DefaultDir"])
            if name != ".":
                parts.append(name)
            dir_id = row["Directory_Parent"]
        return list(reversed(parts))

    return [
        (dir_path(components[row["Component_"]]["Directory_"]), long_name(row["FileName"]),
         os.path.join(cab_dir, row["File"]))
        for row in table("File")
    ]


def msi_property(msi_streams_dir, name):
    for row in read_tables(msi_streams_dir)("Property"):
        if row["Property"] == name:
            return row["Value"]
    return None
