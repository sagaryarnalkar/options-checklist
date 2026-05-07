#!/usr/bin/env python3
"""Simple docx repacker — zips the unpacked tree back into a .docx."""

import os
import sys
import zipfile

src = "/Users/sagary/Claude Work Folder/OptionsStrats/handbook_unpacked"
dst = "/Users/sagary/Downloads/options_strategy_handbook_v5.docx"

with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zf:
    for root, dirs, files in os.walk(src):
        for f in files:
            abspath = os.path.join(root, f)
            relpath = os.path.relpath(abspath, src)
            # docx expects forward slashes inside the archive
            arcname = relpath.replace(os.sep, "/")
            zf.write(abspath, arcname)

print(f"wrote {dst} ({os.path.getsize(dst)} bytes)")
