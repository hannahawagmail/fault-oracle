#!/usr/bin/env python3
"""Terminal UI for interactive fault injection (curses with simple fallback)."""
from __future__ import annotations

import os
import subprocess
import sys

SCRIPTS_DIR = os.path.join(os.path.dirname(__file__), "..", "fault-injection")
MENU = "[1] Inject CE  [2] Inject UE  [3] Inject AER  [4] View Metrics  [5] Quit"
COMMANDS: dict[str, list[str]] = {
    "1": [os.path.join(SCRIPTS_DIR, "inject_edac_ce.sh"), "--backend", "none"],
    "2": [os.path.join(SCRIPTS_DIR, "inject_edac_ue.sh"), "--backend", "none"],
    "3": [os.path.join(SCRIPTS_DIR, "inject_aer.sh"), "--backend", "none"],
    "4": ["bash", "-c", "curl -s localhost:9101/metrics | grep -E 'edac_|aer_|mce_' | head -20"],
}


def run_cmd(key: str) -> str:
    try:
        r = subprocess.run(COMMANDS[key], capture_output=True, text=True, timeout=10)
        return r.stdout + r.stderr or "(no output)"
    except Exception as e:
        return f"Error: {e}"


def fallback_loop() -> None:
    while True:
        print(f"\n{MENU}")
        choice = input(">>> ").strip()
        if choice == "5":
            break
        if choice in COMMANDS:
            print(run_cmd(choice))
        else:
            print("Invalid choice.")


def curses_main(stdscr) -> None:
    import curses
    curses.curs_set(0)
    output_lines: list[str] = ["Ready. Select an option."]
    scroll = 0

    def draw() -> None:
        stdscr.clear()
        h, w = stdscr.getmaxyx()
        stdscr.addnstr(0, 0, MENU, w - 1)
        visible = output_lines[scroll:scroll + h - 2]
        for i, line in enumerate(visible, start=2):
            if i >= h:
                break
            stdscr.addnstr(i, 0, line, w - 1)
        stdscr.refresh()

    while True:
        draw()
        try:
            key = stdscr.getkey()
        except Exception:
            continue
        if key == "5" or key == "q":
            break
        if key in COMMANDS:
            output_lines = run_cmd(key).splitlines() or ["(empty)"]
            scroll = 0
        elif key == "KEY_DOWN" and scroll < len(output_lines) - 1:
            scroll += 1
        elif key == "KEY_UP" and scroll > 0:
            scroll -= 1
        elif key == "KEY_RESIZE":
            pass


def main() -> None:
    if "--non-interactive" in sys.argv:
        print(MENU)
        return
    try:
        import curses
        curses.wrapper(curses_main)
    except (ImportError, curses.error):
        fallback_loop()


if __name__ == "__main__":
    main()
