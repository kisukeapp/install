#!/usr/bin/env python3
"""
Kisuke Broker - Main entry point.

WebSocket broker for iOS <-> Claude Code communication with proxy support.
This broker manages sessions, routes, and message flow between iOS clients
and Claude instances through an Anthropic-compatible proxy.
"""

import asyncio
import sys
import os
from pathlib import Path

# Add current directory to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from broker import KisukeBroker
from broker.config import PORT
from broker.utils import setup_logging

def main():
    """Main entry point."""
    # Setup logging (KISUKE_DEBUG=1 forces DEBUG level)
    setup_logging()
    
    # Use configured port (BROKER_PORT env is read in broker.config)
    port = PORT

    # Allow host override via KHOST env (passed inline by iOS)
    host = os.getenv("KHOST")
    if host:
        print(f"Using KHOST={host} for broker binding")

    # Create and run broker with host binding (falls back to 127.0.0.1 if needed)
    broker = KisukeBroker(port=port, host=host)
    
    try:
        asyncio.run(broker.run_forever())
    except KeyboardInterrupt:
        print("\nShutting down...")
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
