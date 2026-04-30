import sys
from io import TextIOWrapper

# Reconfigure stdout/stderr to UTF-8 before any module that creates a
# Rich Console — otherwise Windows cp1250 rejects non-ASCII characters.
if isinstance(sys.stdout, TextIOWrapper):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if isinstance(sys.stderr, TextIOWrapper):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

if sys.platform == "win32":
    import colorama

    colorama.just_fix_windows_console()

from alt_ani_cli.cli import main

main()
