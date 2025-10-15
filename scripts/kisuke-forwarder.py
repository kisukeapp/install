#!/usr/bin/env python3
"""Port forwarding monitor for Kisuke iOS integration.

Monitors tmux terminal sessions for development server URLs and streams
port forwarding events to iOS clients via JSON over stdout. Automatically
detects when development servers start and become available.

Features:
    - Automatic detection of development server URLs in tmux panes
    - Support for multiple protocols (http, https, ws, wss)
    - Framework-specific pattern matching (Vite, Next.js, React, Django, etc.)
    - Port availability checking with configurable grace periods
    - JSON event streaming for iOS integration
    - Graceful handling of port lifecycle (open/close events)

Event Types:
    - FORWARDER_STARTED: Forwarder initialization complete
    - PORT_REQUEST: New port detected and available
    - PORT_CLOSED: Previously detected port no longer active
    - DEBUG_*: Various debug events for troubleshooting
"""

import os
import re
import json
import sys
import time
import socket
import subprocess
import threading
from collections import defaultdict

# Configuration
TMUX_SESSION = os.environ.get("KISUKE_TMUX_SESSION", "kisuke-terminal")  # Tmux session to monitor
CHECK_INTERVAL = 0.2  # Interval between port checks in seconds (fast polling for sub-second detection)

# Regular expression patterns for detecting development server URLs
# Each tuple contains (pattern, default_protocol) where default_protocol
# is used when the pattern doesn't capture the protocol
URL_PATTERNS = [
    # Vite, Vue, and React development servers
    (re.compile(r'Local:\s+(https?)://localhost:(\d+)'), None),
    (re.compile(r'-\s+Local:\s+(https?)://localhost:(\d+)'), None),  # Next.js 14+ with flexible spacing
    (re.compile(r'➜\s+Local:\s+(https?)://localhost:(\d+)'), None),
    (re.compile(r'running at:\s+(https?)://localhost:(\d+)'), None),
    
    # Next.js framework patterns
    (re.compile(r'ready on (https?)://localhost:(\d+)'), None),
    (re.compile(r'started server on .*:(https?)://.*:(\d+)'), None),
    (re.compile(r'ready on .*:(\d+)'), 'http'),  # Default protocol: http
    (re.compile(r'started server on .*:(\d+)'), 'http'),
    
    # Create React App patterns
    (re.compile(r'On Your Network:\s+(https?)://.*:(\d+)'), None),
    (re.compile(r'Compiled successfully!.*(https?)://localhost:(\d+)'), None),
    
    # WebSocket server patterns
    (re.compile(r'WebSocket server listening on port (\d+)'), 'ws'),
    (re.compile(r'WS server on port (\d+)'), 'ws'),
    (re.compile(r'WSS server on port (\d+)'), 'wss'),
    
    # Generic server patterns
    (re.compile(r'Server listening on port (\d+)'), 'http'),
    (re.compile(r'Listening on port (\d+)'), 'http'),
    (re.compile(r'Server running on port (\d+)'), 'http'),
    (re.compile(r'(https?)://(?:localhost|127\.0\.0\.1):(\d+)'), None),
    
    # Python framework patterns
    (re.compile(r'Django version .* using .* on (https?)://.*:(\d+)'), None),
    (re.compile(r'Flask .* on (https?)://.*:(\d+)'), None),
    (re.compile(r'Uvicorn running on (https?)://.*:(\d+)'), None),
]

class TmuxMonitor:
    """Monitors tmux panes for development server URLs.
    
    Continuously scans tmux pane output for patterns indicating
    development servers have started, checks port availability,
    and emits JSON events for iOS client consumption.
    
    Attributes:
        detected_ports: Dictionary tracking detected ports and their metadata.
        running: Flag indicating if monitor is active.
    """
    def __init__(self):
        self.detected_ports = defaultdict(dict)
        self.emitted_broker_ports = set()  # Track broker events to avoid spam
        self.broker_panes = set()  # Track panes running the broker
        self.running = True
        self.emit_lock = threading.Lock()  # Ensure atomic JSON writes
        # Initialize and send startup event
        self.emit_event({
            'type': 'FORWARDER_STARTED',
            'tmux_session': TMUX_SESSION,
            'pid': os.getpid()
        })
        
    def emit_event(self, event):
        """Emit JSON event to stdout.

        Sends a JSON-formatted event to stdout for iOS client consumption.
        Handles broken pipe errors gracefully by stopping the monitor.
        Uses a lock to ensure atomic writes and prevent JSON interleaving.

        Args:
            event: Dictionary containing event data to emit.
        """
        try:
            with self.emit_lock:
                # Create complete JSON line before writing to prevent interleaving
                json_line = json.dumps(event) + '\n'
                sys.stdout.write(json_line)
                sys.stdout.flush()
        except (BrokenPipeError, IOError):
            # iOS client disconnected - stop monitoring
            self.running = False
        
    def get_tmux_panes(self):
        """Get list of all tmux panes in the monitored session.
        
        Returns:
            List of pane IDs in the tmux session, or empty list on error.
        """
        try:
            result = subprocess.run(
                ['tmux', 'list-panes', '-t', TMUX_SESSION, '-s', '-F', '#{pane_id}'],
                capture_output=True,
                text=True
            )
            
            if result.returncode == 0:
                return result.stdout.strip().split('\n')
            else:
                return []
        except Exception:
            return []
            
    def capture_pane_output(self, pane_id):
        """Capture recent output from a specific tmux pane.

        Args:
            pane_id: Tmux pane identifier.

        Returns:
            String containing last 500 lines of pane output, or empty string on error.
        """
        try:
            result = subprocess.run(
                ['tmux', 'capture-pane', '-t', pane_id, '-p', '-S', '-500'],
                capture_output=True,
                text=True
            )
            
            if result.returncode == 0:
                return result.stdout
            else:
                return ""
        except Exception:
            return ""
            
    def extract_ports(self, text, pane_id):
        """Extract port numbers and protocols from text.

        Scans text for patterns matching development server URLs and
        extracts port numbers with their associated protocols.
        Also detects broker readiness signals and tracks broker panes.

        Args:
            text: Text to scan for port patterns.
            pane_id: ID of the pane being scanned.

        Returns:
            Dictionary mapping port numbers to (protocol, path) tuples.
        """
        ports = {}  # Dictionary mapping port to (protocol, path)

        # Check for broker ready signal (event-driven startup)
        # Only emit once per port to avoid spam
        broker_match = re.search(r'BROKER_READY:(\d+)', text)
        if broker_match:
            port = int(broker_match.group(1))
            if port not in self.emitted_broker_ports:
                self.emitted_broker_ports.add(port)
                self.broker_panes.add(pane_id)  # Mark this pane as running broker
                self.emit_event({
                    'type': 'BROKER_READY',
                    'port': port
                })
            # Don't scan broker panes for other URLs
            return ports

        for pattern, default_protocol in URL_PATTERNS:
            matches = pattern.findall(text)
            for match in matches:
                try:
                    if isinstance(match, tuple):
                        # Pattern captured protocol and port
                        if len(match) == 2:
                            if match[0].isdigit():
                                # Pattern captured port as first group
                                port = int(match[0])
                                protocol = default_protocol or 'http'
                            else:
                                # Pattern captured protocol first, then port
                                protocol = match[0]
                                port = int(match[1])
                        else:
                            continue
                    else:
                        # Single capture group contains port number
                        port = int(match)
                        protocol = default_protocol or 'http'

                    if 1024 <= port <= 65535:  # Check valid port range
                        ports[port] = (protocol, "/")
                except (ValueError, IndexError):
                    pass

        return ports
        
    def check_port_active(self, port):
        """Check if a port is actively listening.
        
        Attempts to connect to the specified port on localhost
        to verify it's accepting connections.
        
        Args:
            port: Port number to check.
            
        Returns:
            True if port is listening, False otherwise.
        """
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1)
            result = sock.connect_ex(('localhost', port))
            sock.close()
            return result == 0
        except:
            return False
            
            
    def monitor_panes(self):
        """Main monitoring loop for detecting development servers.

        Continuously scans tmux panes for server URLs, tracks port
        lifecycle, and emits appropriate events. Handles both newly
        detected ports and ports that have closed.
        """
        while self.running:
            try:
                panes = self.get_tmux_panes()

                for pane_id in panes:
                    # Skip broker panes - they shouldn't be scanned for dev server URLs
                    if pane_id in self.broker_panes:
                        continue

                    output = self.capture_pane_output(pane_id)
                    detected_ports_info = self.extract_ports(output, pane_id)

                    for port, (protocol, path) in detected_ports_info.items():
                        if port not in self.detected_ports:
                            # Process newly detected port
                            port_active = self.check_port_active(port)

                            # Development servers may not be immediately accessible
                            # Forward port info optimistically for common dev ports
                            if port_active or (3000 <= port <= 9999):
                                self.detected_ports[port] = {
                                    'pane_id': pane_id,
                                    'protocol': protocol,
                                    'path': path,
                                    'detected_at': time.time()
                                }

                                # Send port forwarding request to iOS
                                self.emit_event({
                                    'type': 'PORT_REQUEST',
                                    'port': port,
                                    'protocol': protocol,
                                    'path': path
                                })

                # Verify previously detected ports are still active
                for port in list(self.detected_ports.keys()):
                    port_info = self.detected_ports[port]
                    port_age = time.time() - port_info['detected_at']
                    port_active = self.check_port_active(port)

                    # Development servers often announce URLs before listening
                    # Allow grace period before considering port closed
                    if 3000 <= port <= 9999:
                        grace_period = 30  # Extended grace for dev ports
                    else:
                        grace_period = 10  # Standard grace period

                    # Handle ports transitioning from inactive to active
                    if port_active and not port_info.get('was_active', False):
                        port_info['was_active'] = True
                        # Send port request now that port is active
                        self.emit_event({
                            'type': 'PORT_REQUEST',
                            'port': port,
                            'protocol': port_info['protocol'],
                            'path': port_info['path']
                        })
                    elif port_active:
                        port_info['was_active'] = True

                    if not port_active and port_age > grace_period:
                        # Port closed - remove and notify iOS
                        del self.detected_ports[port]

                        # Notify iOS that port is no longer available
                        self.emit_event({
                            'type': 'PORT_CLOSED',
                            'port': port,
                            'protocol': port_info['protocol']
                        })

            except KeyboardInterrupt:
                self.running = False
                break
            except Exception:
                # Continue monitoring on errors to maintain stream
                pass

            time.sleep(CHECK_INTERVAL)
            
    def run(self):
        """Start the port forwarding monitor.
        
        Begins the monitoring loop and handles graceful shutdown
        on interruption or pipe closure.
        """
        # Begin monitoring tmux panes
        try:
            self.monitor_panes()
        except (KeyboardInterrupt, BrokenPipeError, IOError):
            # Graceful shutdown on interrupt or disconnection
            pass
        finally:
            self.running = False

def main():
    """Main entry point for the port forwarder.
    
    Creates and runs the tmux monitor. Output is automatically
    unbuffered for real-time event streaming to iOS clients.
    The iOS client manages the process lifecycle.
    """
    monitor = TmuxMonitor()
    monitor.run()

if __name__ == "__main__":
    main()
