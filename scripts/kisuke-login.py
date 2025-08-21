#!/usr/bin/env python3
"""Headless OAuth login helper for Claude CLI.

Provides automated login functionality for the Claude CLI by handling
the OAuth flow in a headless environment. Outputs JSON events to stdout
for integration with iOS clients.

The script automates the Claude CLI login process by:
1. Spawning the Claude CLI in a pseudo-terminal
2. Automatically selecting theme and authentication method
3. Extracting the OAuth URL for user authentication
4. Accepting the OAuth code and completing authentication

Output Events (JSON to stdout):
    - {"event": "oauth_url", "url": "..."}: OAuth URL for user login
    - {"event": "login_ok"}: Authentication successful
    - {"event": "error", "msg": "..."}: Error occurred
    - {"event": "status", "msg": "..."}: Status updates
    - {"event": "debug", "msg": "..."}: Debug information
"""

import os
import pty
import select
import re
import sys
import json
import argparse
import tty
import signal
import time
import shutil

# Possible locations for Claude CLI executable
POSSIBLE = [
    os.path.expanduser("~/.kisuke/cli/node_modules/.bin/claude"),
    os.path.expanduser("~/.kisuke/bin/claude"),
]

# Regular expressions for parsing CLI output
ANSI = re.compile(r'\x1B\[[0-?]*[ -/]*[@-~]')  # ANSI escape sequences
URL  = re.compile(r'https://\S+')              # OAuth URLs

# Timing configuration
RESEND = 2.0  # Seconds between Enter key resends while waiting for URL

def find_cli() -> str:
    """Locate the Claude CLI executable.
    
    Searches for the Claude CLI in known locations and system PATH.
    
    Returns:
        Path to the Claude CLI executable.
        
    Raises:
        SystemExit: If Claude CLI cannot be found.
    """
    for p in POSSIBLE:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    # Check system PATH as fallback
    found = shutil.which("claude")
    if found:
        return found
    
    # CLI not found - emit error and exit
    emit({"event": "error", "msg": "Claude CLI not found on PATH or ~/.kisuke"})
    sys.exit(1)

def emit(obj):
    """Emit JSON event to stdout.
    
    Args:
        obj: Dictionary to serialize as JSON.
    """
    print(json.dumps(obj), flush=True)

def run(code, verbose):
    """Run the Claude CLI login process.
    
    Spawns the Claude CLI in a pseudo-terminal, handles the interactive
    login flow, and manages OAuth authentication.
    
    Args:
        code: Optional OAuth code to automatically submit.
        verbose: If True, output CLI UI to stderr for debugging.
    """
    # Configure environment to prevent browser launch
    env = {**os.environ, "CI": "1", "BROWSER": "echo"}
    # Fork and execute Claude CLI in pseudo-terminal
    pid, fd = pty.fork()
    if pid == 0:
        # Child process: execute Claude CLI
        os.execvpe(find_cli(), ["claude", "/login"], env)

    # Set up polling for CLI output
    poll = select.poll()
    poll.register(fd, select.POLLIN)
    
    # Register stdin for manual OAuth code entry
    if code is None:
        poll.register(sys.stdin, select.POLLIN)
        emit({"event": "debug", "msg": "Stdin registered for polling"})

    # Configure terminal for interactive input if available
    old = None
    if code is None and sys.stdin.isatty():
        # Save current terminal settings and enable cbreak mode
        old = tty.tcgetattr(sys.stdin.fileno())
        tty.setcbreak(sys.stdin)
        emit({"event": "debug", "msg": "Terminal set to cbreak mode"})
    elif code is None:
        emit({"event": "debug", "msg": "No TTY detected, using standard stdin"})

    # Set up signal handler for clean shutdown
    def interrupt_handler(*_):
        emit({"event": "error", "msg": "Interrupted"})
        os.kill(pid, signal.SIGTERM)
        sys.exit(1)
    signal.signal(signal.SIGINT, interrupt_handler)

    # State tracking for login flow
    url_done = False         # OAuth URL extracted
    last_enter = 0          # Last time Enter was sent
    theme_selected = False  # Theme selection completed
    method_selected = False # Auth method selected
    
    try:
        while True:
            # Poll for I/O with 100ms timeout
            ready = poll.poll(100)
            if not url_done and time.time()-last_enter > RESEND:
                os.write(fd, b"\r"); last_enter = time.time()
            
            # Log polling activity for debugging
            if ready and url_done:
                emit({"event": "debug", "msg": f"Poll ready: {ready}"})

            for h, _ in ready:
                if h == fd:  # Handle CLI output
                    raw = os.read(fd, 8192).decode("utf-8", "ignore")
                    if not raw:
                        emit({"event": "error", "msg": "CLI exited"})
                        return
                    # Remove ANSI escape sequences for parsing
                    txt = ANSI.sub("", raw)
                    if verbose:
                        sys.stderr.write(txt)
                        sys.stderr.flush()

                    # Automatically select dark mode theme
                    if not theme_selected and "Choose the text style" in txt:
                        time.sleep(0.5)  # Delay for UI update
                        os.write(fd, b"1\r")
                        theme_selected = True
                        emit({"event": "status", "msg": "Selected dark mode theme"})
                    
                    # Automatically select subscription authentication
                    elif not method_selected and "Select login method:" in txt:
                        time.sleep(0.5)  # Delay for UI update
                        os.write(fd, b"1\r")
                        method_selected = True
                        emit({"event": "status", "msg": "Selected subscription login method"})
                    
                    # Extract OAuth URL from output
                    elif not url_done and (m := URL.search(txt)):
                        emit({"event": "oauth_url", "url": m.group(0)})
                        url_done = True
                        # Auto-submit OAuth code if provided
                        if code:
                            os.write(fd, f"{code}\r".encode())

                    if "Authentication successful" in txt:
                        emit({"event": "login_ok"})
                        return

                else:  # Handle manual OAuth code input from stdin
                    if url_done:
                        # Read OAuth code from stdin (can be long)
                        cd = os.read(sys.stdin.fileno(), 1024).decode().strip()
                        if cd:
                            emit({"event": "status", "msg": f"Received OAuth code from stdin (length: {len(cd)})"})
                            # Submit OAuth code to CLI
                            os.write(fd, cd.encode() + b"\r")
                            emit({"event": "status", "msg": "OAuth code sent to CLI"})
    finally:
        # Restore original terminal settings
        if old is not None:
            tty.tcsetattr(sys.stdin.fileno(), tty.TCSADRAIN, old)

if __name__ == "__main__":
    # Parse command-line arguments
    ap = argparse.ArgumentParser(
        description="Headless OAuth login helper for Claude CLI")
    ap.add_argument("--code",
                    help="OAuth authorization code to automatically submit")
    ap.add_argument("--verbose",
                    action="store_true",
                    help="Output CLI UI to stderr for debugging")
    args = ap.parse_args()
    
    # Run login process with error handling
    try:
        run(args.code, args.verbose)
    except Exception as e:
        emit({"event": "error", "msg": str(e)})
        sys.exit(1)

