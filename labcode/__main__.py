"""Enable ``python -m labcode <command> ...``.

Intent: mirror the console-script entry point so the CLI is reachable without an
installed script, which is convenient in dev checkouts and CI.
"""

from labcode.cli import main

raise SystemExit(main())
