"""
Main broker class that coordinates all components.
"""
import asyncio
import json
import logging
import sys
from pathlib import Path
import websockets

# Add proxy module to path if needed
sys.path.insert(0, str(Path(__file__).parent.parent))

from .config import *
from .utils import setup_logging, get_claude_cli_path
from .core.session_manager import SessionManager
from .core.connection_manager import ConnectionManager
from .core.message_buffer import MessageBuffer
from .core.ack_manager import AckManager
from .routes import RouteManager
from .claude_interface import ClaudeInterface
from .handlers import MessageHandlers
from .permission_manager import RuntimePermissionManager, PermissionMode

log = logging.getLogger(__name__)

class KisukeBroker:
    """Main broker coordinating iOS <-> Claude communication."""
    
    def __init__(self, port: int = PORT):
        """
        Initialize Kisuke Broker.
        
        Args:
            port: WebSocket port to listen on
        """
        self.port = port
        self.running = False
        self.global_credentials = None  # Global credentials from iOS (shared by all sessions)
        
        # Initialize core components
        self.connection_manager = ConnectionManager(
            max_connections_per_session=3,
            connection_timeout=0,  # Never timeout (persistent connections)
            cleanup_interval=30
        )
        self.message_buffer = MessageBuffer(
            max_buffer_size=1000,
            retention_time=300  # 5 minutes
        )
        self.ack_manager = AckManager()  # Initialize ACK manager
        self.session_manager = SessionManager(
            connection_manager=self.connection_manager,
            message_buffer=self.message_buffer,
            session_timeout=0,  # Never timeout (persistent sessions)
            cleanup_interval=60,
            global_credentials=None  # Will be set when credentials arrive from iOS
        )
        # Store ack_manager reference in session_manager for sequence tracking
        self.session_manager.ack_manager = self.ack_manager

        # Initialize permission manager (starts in PROMPT mode)
        self.permission_manager = RuntimePermissionManager(
            initial_mode=PermissionMode.PROMPT
        )

        # Initialize other components
        self.route_manager = RouteManager()
        self.claude_interface = ClaudeInterface(
            permission_manager=self.permission_manager,
            default_base_url=DEFAULT_ANTHROPIC_BASE_URL
        )

        # Store claude_interface reference in session_manager for process termination
        self.session_manager.claude_interface = self.claude_interface

        # Initialize handlers (pass broker instance for global credentials)
        self.message_handlers = MessageHandlers(
            broker=self,  # Pass broker instance for global credential access
            session_manager=self.session_manager,
            connection_manager=self.connection_manager,
            message_buffer=self.message_buffer,
            route_manager=self.route_manager,
            claude_interface=self.claude_interface,
            ack_manager=self.ack_manager
        )

        # Wire iOS permission handler to permission manager
        self.permission_manager.set_ios_handler(
            self.message_handlers.send_permission_request_to_ios
        )
        log.info("Wired iOS permission handler to RuntimePermissionManager")

        # Proxy runner
        self.proxy_runner = None
        # WebSocket server
        self.ws_server = None
    
    async def start(self):
        """Start the broker and all components."""
        if self.running:
            log.warning("Broker already running")
            return
        
        log.info("Starting Kisuke Broker...")

        # Start WebSocket server first for fastest readiness signal
        async def connection_handler(websocket, path=None):
            await self.message_handlers.handle_connection(websocket, path or "/")

        self.ws_server = await websockets.serve(
            connection_handler,
            HOST,
            self.port,
            ping_interval=None,  # Disable server-initiated pings - iOS manages heartbeat
            ping_timeout=None,   # No timeout for pongs - permanent connections
            close_timeout=10,    # Clean shutdown timeout only
            max_size=10 * 1024 * 1024  # 10 MB max frame size (matches client)
        )
        log.info(f"WebSocket server listening on ws://{HOST}:{self.port}")

        # Print to stdout for forwarder detection (event-driven startup)
        print(f"BROKER_READY:{self.port}", flush=True)

        # Kick off remaining startup tasks concurrently (proxy + managers)
        # These are lightweight and don't block accepting connections.
        # They are required before handling session/SDK flows, but not for the initial handshake.
        await asyncio.gather(
            self._start_proxy(),
            self.connection_manager.start(),
            self.message_buffer.start(),
            self.session_manager.start(),
        )

        # Optional: log Claude CLI location (non-blocking check)
        claude_path = get_claude_cli_path()
        if not claude_path:
            log.warning("Claude CLI not found - make sure it's installed")
        else:
            log.info(f"Using Claude CLI at {claude_path}")

        self.running = True
    
    async def stop(self):
        """Stop the broker and all components."""
        if not self.running:
            return
        
        log.info("Stopping Kisuke Broker...")
        
        # Stop WebSocket server
        if self.ws_server:
            self.ws_server.close()
            await self.ws_server.wait_closed()
        
        # Stop components
        await self.session_manager.stop()
        await self.connection_manager.stop()
        await self.message_buffer.stop()
        await self.claude_interface.close_all()
        
        # Stop proxy
        await self._stop_proxy()
        
        self.running = False
        log.info("Kisuke Broker stopped")
    
    async def run_forever(self):
        """Run the broker until interrupted."""
        await self.start()
        
        try:
            # Keep running until interrupted
            while self.running:
                await asyncio.sleep(1)
        except KeyboardInterrupt:
            log.info("Received interrupt signal")
        finally:
            await self.stop()
    
    async def _start_proxy(self):
        """Start the embedded proxy server."""
        try:
            from kisuke_proxy import start_proxy
            
            self.proxy_runner = await start_proxy(PROXY_HOST, PROXY_PORT)
            log.info(f"Embedded Claude proxy listening on http://{PROXY_HOST}:{PROXY_PORT}")
            
        except ImportError:
            log.warning("Proxy module not found - running without proxy")
        except Exception as e:
            log.error(f"Failed to start proxy: {e}")
    
    async def _stop_proxy(self):
        """Stop the embedded proxy server."""
        if self.proxy_runner:
            try:
                await self.proxy_runner.cleanup()
                log.info("Proxy stopped")
            except Exception as e:
                log.error(f"Error stopping proxy: {e}")
    

async def main():
    """Main entry point for the broker."""
    setup_logging(LOG_LEVEL)
    
    broker = KisukeBroker()
    await broker.run_forever()

if __name__ == "__main__":
    asyncio.run(main())
