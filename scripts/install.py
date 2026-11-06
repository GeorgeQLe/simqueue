"""Install the local launcher and periodic sweeper definition, without starting it."""

import argparse
import os
import plistlib
import shlex
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    executable = repo / ".venv/bin/simqueue"
    if not executable.is_file():
        raise SystemExit("Run uv sync --locked in the external storage checkout first")
    launcher = Path.home() / ".local/bin/simqueue"
    marker = "# simqueue managed launcher"
    if launcher.exists() and marker not in launcher.read_text():
        raise SystemExit("Existing unrelated simqueue launcher; left untouched")
    if not args.apply:
        print(f"Would install {launcher} and the user simqueue sweep LaunchAgent; use --apply")
        return
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text(
        "#!/bin/sh\n" + marker + "\nexec " + shlex.quote(str(executable)) + ' "$@"\n'
    )
    launcher.chmod(0o755)
    plist = Path.home() / "Library/LaunchAgents/com.georgele.simqueue.sweep.plist"
    plist.parent.mkdir(parents=True, exist_ok=True)
    definition = {
        "Label": "com.georgele.simqueue.sweep",
        "ProgramArguments": [str(launcher), "sweep"],
        "StartInterval": 60,
        "RunAtLoad": True,
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "StandardOutPath": "/dev/null",
        "StandardErrorPath": "/dev/null",
    }
    if plist.exists() and plistlib.loads(plist.read_bytes()).get("Label") != definition["Label"]:
        raise SystemExit("Existing unrelated LaunchAgent; left untouched")
    plist.write_bytes(plistlib.dumps(definition))
    plist.chmod(0o600)
    print(f"Installed {launcher} and {plist}; load the LaunchAgent separately")
    print(f"GUI domain: gui/{os.getuid()}")


if __name__ == "__main__":
    main()
