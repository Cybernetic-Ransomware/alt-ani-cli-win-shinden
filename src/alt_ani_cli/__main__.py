import sys

# Reconfigure stdout/stderr to UTF-8 before any module that creates a
# Rich Console — otherwise Windows cp1250 rejects non-ASCII characters.
if hasattr(sys.stdout, "reconfigure"):
    getattr(sys.stdout, "reconfigure")(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    getattr(sys.stderr, "reconfigure")(encoding="utf-8", errors="replace")

if sys.platform == "win32":
    import colorama

    colorama.just_fix_windows_console()

from alt_ani_cli.cli import main

main()
