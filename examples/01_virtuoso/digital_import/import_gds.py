#!/usr/bin/env python3
"""Import a routed GDS into a Virtuoso library via Cadence ``strmin``.

The standalone ``strmin`` tool is invoked through SKILL ``system()`` —
strmin inherits the running Virtuoso's PATH, licence env, and working
directory, so no SSH or local-shell setup is needed.

Prerequisites
-------------
* ``virtuoso-bridge start`` is running, daemon loaded in CIW.
* The target library is already DEFINEd in the Virtuoso work dir's
  ``cds.lib``.  ``strmin`` creates the cellview directories but does
  not amend ``cds.lib``.

Reference libraries
-------------------
* ``--ref-libs <file>`` (recommended) — plain text file listing the
  referenced lib names, one per line.  Lab convention is
  ``<workdir>/ref``.  Keeps import scope explicit and auditable.
* ``--use-cds-lib`` — shortcut for strmin's magic ``-refLibList
  XST_CDS_LIB``: refs **every** lib in the work dir's cds.lib
  (including ``INCLUDE`` chains).  Unsafe unless the cds.lib is
  strictly curated — same-name cells across PDK / IP / historical
  libs will silently bind to the wrong one.

The script prints instance/shape counts of the new layout cellview as a
sanity check after import.
"""

from __future__ import annotations

import argparse
import shlex
import sys
import time
from pathlib import Path

from virtuoso_bridge import VirtuosoClient
from virtuoso_bridge.virtuoso.ops import escape_skill_string


def _q(s: str) -> str:
    """Wrap a Python string as a SKILL string literal."""
    return f'"{escape_skill_string(s)}"'


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 2)[0])
    parser.add_argument(
        "gds",
        help="Path to the .gds file (must be readable by the Virtuoso process)",
    )
    parser.add_argument(
        "--target-lib", required=True,
        help="OA library to write into (must already be DEFINEd in cds.lib)",
    )
    parser.add_argument(
        "--tech-lib", default="tsmcN28",
        help="OA library that supplies the tech file (default: tsmcN28)",
    )
    refgrp = parser.add_mutually_exclusive_group()
    refgrp.add_argument(
        "--ref-libs", default=None,
        help="(recommended) File path passed to strmin -refLibList — a "
             "plain text file with one referenced lib name per line "
             "(e.g. tcbn28hpcplus..., tphn28hpcpgv18...).  Mutually "
             "exclusive with --use-cds-lib.",
    )
    refgrp.add_argument(
        "--use-cds-lib", action="store_true",
        help="UNSAFE shortcut for `-refLibList XST_CDS_LIB`: refs every "
             "lib in the work dir's cds.lib (incl. INCLUDE chains).  "
             "Risk: same-name cells across PDK / IP / old project libs "
             "will silently bind to the wrong one.  Use only with a "
             "strictly curated cds.lib; prefer --ref-libs.  Mutually "
             "exclusive with --ref-libs.",
    )
    parser.add_argument(
        "--cell", default=None,
        help="Override the cell name to verify after import "
             "(default: stem of the GDS file, splitting on '.')",
    )
    args = parser.parse_args()

    # MSYS / Git Bash path-mangling check.  Git Bash on Windows
    # rewrites Linux-style /home/... into <msys_root>/home/... before
    # argv reaches Python — strmin then can't find the file and the
    # error message points at the *mangled* path, which is confusing.
    # Detect and bail with a clear pointer to the fix.
    if args.gds.startswith(("C:/", "C:\\", "D:/", "D:\\")) and "/home/" in args.gds.replace("\\", "/"):
        unmangled = "/home/" + args.gds.replace("\\", "/").split("/home/", 1)[1]
        sys.exit(
            "ERROR: GDS path appears mangled by Git Bash / MSYS / Cygwin.\n"
            f"  Received:  {args.gds}\n"
            f"  Expected:  {unmangled}\n"
            "  Cause:     these shells translate Linux paths like /home/... to "
            "<msys_root>/home/... before Python sees the argv.\n"
            "  Fix:       on Windows, run this script from PowerShell, cmd.exe, "
            "or WSL — NOT Git Bash.\n"
            "             (Claude Code: prefer the PowerShell tool over Bash for "
            "this script on Windows hosts.)"
        )

    client = VirtuosoClient.from_env()

    # 1. Make sure the target library is registered in cds.lib.
    r = client.execute_skill(
        f'sprintf(nil "%L" ddGetObj({_q(args.target_lib)})~>readPath)'
    )
    if (r.output or "").strip() in ('"nil"', "nil", ""):
        sys.exit(
            f"ERROR: library '{args.target_lib}' is not in Virtuoso's cds.lib.\n"
            f"  Add a 'DEFINE {args.target_lib} <path>' line and restart Virtuoso, "
            f"or call ddUpdateLibList() first."
        )

    # 2. Compose the strmin command line.  Use shlex.quote so paths with
    #    spaces or odd chars survive the trip through SKILL's system().
    parts = [
        "strmin",
        "-library",            shlex.quote(args.target_lib),
        "-strmFile",           shlex.quote(args.gds),
        "-attachTechFileOfLib", shlex.quote(args.tech_lib),
        "-logFile",            "strmIn.log",
    ]
    if args.use_cds_lib:
        # XST_CDS_LIB is a magic literal that strmin understands as
        # "use every lib defined in the cds.lib resolved from cwd".
        # Not a path — must NOT be shell-quoted as a filename.
        parts += ["-refLibList", "XST_CDS_LIB"]
    elif args.ref_libs:
        parts += ["-refLibList", shlex.quote(args.ref_libs)]
    parts.append("-replaceBusBitChar")
    cmd = " ".join(parts)

    print(f"[strmin] {cmd}")
    # SKILL system() return is unreliable for strmin — observed
    # 2026-05-13: strmin keeps running on the remote after Python
    # receives empty / garbled rc, so sequential strmin calls race
    # on lib state ("library X is not in cds.lib" from the second
    # call while the first hasn't committed).  Don't trust rc;
    # poll for the target cellview to appear instead.
    client.execute_skill(f"system({_q(cmd)})")

    cell = args.cell or Path(args.gds).name.split(".")[0]
    # ddGetObj-first gate: dbOpenCellViewByType in "r" mode prints
    # `WARNING (DB-270212)` to CIW each time the view is missing.
    # During the poll loop that's a CIW warning every 3 s until the
    # view appears — observed 16+ noise lines per import.  ddGetObj
    # is silent when the view doesn't exist yet, so use it as a
    # cheap gate and only open the cv when ddGetObj confirms it.
    verify_skill = (
        f"let((vobj cv) "
        f"  vobj=ddGetObj({_q(args.target_lib)} {_q(cell)} \"layout\") "
        f"  if(vobj "
        f"     progn( "
        f"       cv=dbOpenCellViewByType({_q(args.target_lib)} {_q(cell)} \"layout\" nil \"r\") "
        f"       if(cv "
        f"          sprintf(nil \"instances=%d shapes=%d bbox=%L\" "
        f"                  length(cv~>instances) length(cv~>shapes) cv~>bBox) "
        f"          nil)) "
        f"     nil)) "
    )

    timeout_s = 600
    poll_interval = 3
    deadline = time.time() + timeout_s
    next_log = time.time() + 30
    while True:
        # Refresh lib list so Library Manager sees newly-written cells.
        client.execute_skill("ddUpdateLibList()")
        r = client.execute_skill(verify_skill)
        out = (r.output or "").strip().strip('"')
        if out.startswith("instances="):
            print(f"[OK] {args.target_lib}/{cell}/layout: {out}")
            return 0
        now = time.time()
        if now >= deadline:
            sys.exit(
                f"strmin: {args.target_lib}/{cell}/layout did not appear "
                f"within {timeout_s}s. Check strmIn.log in Virtuoso's "
                f"working directory."
            )
        if now >= next_log:
            elapsed = int(now - (deadline - timeout_s))
            print(f"[wait] {elapsed}s — strmin still running, polling for cellview...")
            next_log = now + 30
        time.sleep(poll_interval)


if __name__ == "__main__":
    raise SystemExit(main())
