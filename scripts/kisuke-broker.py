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
from broker.config import PORT, LOG_LEVEL
from broker.utils import setup_logging

def main():
    """Main entry point."""
    # Setup logging
    setup_logging(LOG_LEVEL)
    
    # Get port from environment or use default
    port = int(os.getenv("BROKER_PORT", PORT))
    
    # Create and run broker
    broker = KisukeBroker(port=port)
    
    try:
        asyncio.run(broker.run_forever())
    except KeyboardInterrupt:
        print("\nShutting down...")
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()