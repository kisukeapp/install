"""
Claude interface for the broker.

This module provides a simple interface to the Claude SDK,
managing Claude sessions and handling message passing.
"""
import os
import logging
from typing import Dict, Optional, Callable, Any, AsyncGenerator
from dataclasses import dataclass

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
from claude_agent_sdk.types import (
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

from .permission_manager import PermissionMode, RuntimePermissionManager
from .utils import dataclass_to_dict
import uuid

# Debug flag for control message logging
ENABLE_CONTROL_DEBUG = True  # Temporarily forced on for debugging

log = logging.getLogger(__name__)


def _serialize_content_block(block: Any) -> Dict[str, Any]:
    """Normalize Claude content blocks into JSON-serialisable structures."""
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text.strip()}
    if isinstance(block, ThinkingBlock):
        result = {
            "type": "thinking",
            "thinking": block.thinking,
        }
        # Only add signature if it exists
        if hasattr(block, 'signature'):
            result["signature"] = block.signature
        return result
    if isinstance(block, ToolUseBlock):
        return {
            "type": "tool_use",
            "id": block.id,
            "name": block.name,
            "input": dataclass_to_dict(block.input),
        }
    if isinstance(block, ToolResultBlock):
        return {
            "type": "tool_result",
            "tool_use_id": block.tool_use_id,
            "content": dataclass_to_dict(block.content),
            "is_error": block.is_error,
        }
    if isinstance(block, dict):
        return block
    return dataclass_to_dict(block)


def _serialize_message(message: Any) -> Dict[str, Any]:
    """Convert SDK message types into JSON the client can stream."""
    if isinstance(message, AssistantMessage):
        return {
            "type": "assistant",
            "model": message.model,
            "content": [_serialize_content_block(block) for block in message.content],
            "parent_tool_use_id": message.parent_tool_use_id,
        }
    if isinstance(message, UserMessage):
        content = message.content
        if isinstance(content, list):
            content = [_serialize_content_block(block) for block in content]
        return {
            "type": "user",
            "content": content,
            "parent_tool_use_id": message.parent_tool_use_id,
        }
    if isinstance(message, SystemMessage):
        return {
            "type": "system",
            "subtype": message.subtype,
            "data": dataclass_to_dict(message.data),
        }
    if isinstance(message, ResultMessage):
        # Preserve session_id at top level for correlation
        data = dataclass_to_dict(message)
        data["type"] = "result"
        # Ensure session_id is at top level
        if hasattr(message, 'session_id'):
            data["session_id"] = message.session_id
        return data
    if isinstance(message, StreamEvent):
        # Preserve UUID at top level for correlation
        return {
            "type": "stream_event",
            "uuid": message.uuid,
            "session_id": message.session_id,
            "parent_tool_use_id": message.parent_tool_use_id,
            "event": message.event
        }
    if isinstance(message, dict):
        return message
    return {"type": message.__class__.__name__, "data": dataclass_to_dict(message)}

@dataclass
class ClaudeClientConfig:
    """Configuration for Claude client."""
    api_key: str
    model: str = "claude-3.5-sonnet"
    workdir: str = "/tmp"
    system_prompt: Optional[str] = None
    base_url: Optional[str] = None
    permission_mode: str = "prompt"
    resume_id: Optional[str] = None
    add_dirs: Optional[list] = None
    allowed_tools: Optional[list] = None
    disallowed_tools: Optional[list] = None

@dataclass
class ClaudeSession:
    """Wrapper for a Claude SDK session."""
    session_id: str
    client: ClaudeSDKClient
    options: ClaudeAgentOptions
    permission_handler: Optional[Callable] = None
    
    async def send(self, message: str, message_uuid: Optional[str] = None):
        """
        Send a message to Claude.

        Args:
            message: Message content
            message_uuid: Optional UUID for the user message (for editing support)
        """
        query = getattr(self.client, "query", None)
        if callable(query):
            # If message_uuid is provided, send as structured message with UUID
            if message_uuid:
                # Create async generator that yields the message with UUID
                async def message_stream():
                    yield {
                        "type": "user",
                        "uuid": message_uuid,
                        "session_id": self.session_id,
                        "message": {"role": "user", "content": message},
                        "parent_tool_use_id": None
                    }

                await query(message_stream(), session_id=self.session_id)
            else:
                # Send as simple string (SDK will generate UUID)
                await query(message, session_id=self.session_id)
            return None

        send_message = getattr(self.client, "send_message", None)
        if callable(send_message):
            return await send_message(message)

        raise AttributeError("ClaudeSDKClient has no method to send messages")
    
    async def events(self) -> AsyncGenerator[Dict, None]:
        """Stream events from Claude."""
        receive_messages = getattr(self.client, "receive_messages", None)
        if callable(receive_messages):
            async for event in receive_messages():
                yield _serialize_message(event)
            return

        listen = getattr(self.client, "listen", None)
        if callable(listen):
            async for event in listen():
                yield event

    async def send_and_stream_response(self, message: str, callback: Callable, message_uuid: Optional[str] = None):
        """
        Send a message and stream the response through a callback.
        This implements the proper SDK pattern: query followed by receive_response.

        Args:
            message: Message content to send
            callback: Async function to call with each response event
            message_uuid: Optional UUID for message tracking
        """
        # Send the query
        await self.send(message, message_uuid=message_uuid)

        # Stream the response for THIS query
        receive_response = getattr(self.client, "receive_response", None)
        if callable(receive_response):
            async for event in receive_response():
                # Serialize and send to callback
                serialized = _serialize_message(event)
                await callback(serialized)
        else:
            # Fallback to old pattern if receive_response not available
            log.warning("ClaudeSDKClient doesn't have receive_response, falling back to receive_messages")
            receive_messages = getattr(self.client, "receive_messages", None)
            if callable(receive_messages):
                async for event in receive_messages():
                    serialized = _serialize_message(event)
                    await callback(serialized)
                    # Break after first result message to mimic receive_response behavior
                    if isinstance(event, dict) and event.get('type') == 'result':
                        break


def create_can_use_tool_callback(permission_manager: RuntimePermissionManager, tab_id: str):
    """
    Create can_use_tool callback for SDK from RuntimePermissionManager.

    This bridges our RuntimePermissionManager (with iOS integration, caching, modes)
    to the SDK's official permission API.

    Args:
        permission_manager: RuntimePermissionManager instance
        tab_id: Tab ID (encoded in request_id for iOS routing)

    Returns:
        Async callback function compatible with ClaudeAgentOptions.can_use_tool
    """
    async def can_use_tool_callback(
        tool_name: str,
        tool_input: Dict[str, Any],
        context: ToolPermissionContext
    ):
        """
        Permission callback invoked by SDK for each tool use.

        Args:
            tool_name: Name of the tool being requested
            tool_input: Input parameters for the tool
            context: Context with permission suggestions

        Returns:
            PermissionResultAllow or PermissionResultDeny
        """
        # Generate request ID with tab context for iOS routing
        # Format: {tab_id}:{unique_id}
        request_id = f"{tab_id}:{uuid.uuid4().hex[:8]}"

        try:
            # Get decision from permission manager
            decision = await permission_manager.get_permission(
                tool_name=tool_name,
                tool_input=tool_input,
                request_id=request_id
            )

            # Convert to SDK permission result types
            if decision.get("behavior") == "allow":
                # Ensure updatedInput is never None - use original tool_input if not provided
                updated_input = decision.get("updatedInput")
                if updated_input is None:
                    updated_input = tool_input

                return PermissionResultAllow(updated_input=updated_input)
            else:
                return PermissionResultDeny(
                    message=decision.get("reason", "Permission denied"),
                    interrupt=decision.get("interrupt", True)  # Default to True for deny
                )

        except Exception as e:
            log.error(f"Permission callback error for {tool_name}: {e}")
            # Deny on error for safety
            return PermissionResultDeny(
                message=f"Permission system error: {e}"
            )

    return can_use_tool_callback


class ClaudeInterface:
    """
    Interface for managing Claude SDK sessions.
    
    Provides simplified session management and message passing
    for the broker architecture.
    """
    
    def __init__(self,
                 permission_manager: RuntimePermissionManager,
                 default_base_url: str = "https://api.anthropic.com"):
        """
        Initialize Claude interface.

        Args:
            permission_manager: RuntimePermissionManager for handling permissions (required)
            default_base_url: Default base URL for Anthropic API
        """
        self.default_base_url = default_base_url
        self.sessions: Dict[str, ClaudeSession] = {}
        self.permission_manager = permission_manager
    
    async def create_session_with_resume(
        self,
        credentials,
        session_id: str,
        tab_id: str,
        workdir: str = "/tmp",
        system_prompt: Optional[str] = None,
        resume_session_id: Optional[str] = None,
        resume_at_message_uuid: Optional[str] = None,
        permission_handler: Optional[Callable] = None
    ) -> ClaudeSession:
        """
        Create a new Claude session with resume/branching support.

        DEPRECATED: This is now a wrapper around create_session().
        Use create_session() directly with resume parameters.

        Args:
            credentials: ClaudeApiCredentials with API key, model, etc.
            session_id: Session identifier (internal)
            tab_id: Tab identifier (for iOS routing)
            workdir: Working directory
            system_prompt: Optional system prompt
            resume_session_id: Session ID to resume from (for branching)
            resume_at_message_uuid: Message UUID to branch from
            permission_handler: Optional permission request handler

        Returns:
            Created ClaudeSession
        """
        # Wrapper - delegate to create_session with resume parameters
        return await self.create_session(
            credentials=credentials,
            session_id=session_id,
            tab_id=tab_id,
            workdir=workdir,
            system_prompt=system_prompt,
            resume_session_id=resume_session_id,
            resume_at_message_uuid=resume_at_message_uuid,
            permission_handler=permission_handler
        )

    async def create_session(
        self,
        credentials,
        session_id: str,
        tab_id: str,
        workdir: str = "/tmp",
        system_prompt: Optional[str] = None,
        resume_session_id: Optional[str] = None,
        resume_at_message_uuid: Optional[str] = None,
        permission_handler: Optional[Callable] = None
    ) -> ClaudeSession:
        """
        Create a new Claude session with optional resume/branching support.

        Supports 3 modes:
        1. New session: No resume parameters
        2. Resume session: resume_session_id only (continue from where session left off)
        3. Resume at message: resume_session_id + resume_at_message_uuid (branch from specific point)

        Args:
            credentials: ClaudeApiCredentials with API key, model, etc.
            session_id: Session identifier (internal)
            tab_id: Tab identifier (for iOS routing)
            workdir: Working directory
            system_prompt: Optional system prompt
            resume_session_id: Optional session ID to resume from
            resume_at_message_uuid: Optional message UUID to branch from (requires resume_session_id)
            permission_handler: Optional permission request handler

        Returns:
            Created ClaudeSession
        """
        # Set up environment to hijack Claude Code's API calls through our proxy
        # Use session-specific token for per-session credential isolation
        from .config import DEFAULT_ANTHROPIC_BASE_URL
        session_token = f"kisuke-{session_id[:5]}"  # Per-session proxy token
        os.environ["ANTHROPIC_BASE_URL"] = DEFAULT_ANTHROPIC_BASE_URL  # http://127.0.0.1:8082
        os.environ["ANTHROPIC_API_KEY"] = session_token  # Session-specific token

        # Build ClaudeAgentOptions - conditionally include resume parameters
        # Note: We set permission_prompt_tool_name="stdio" to enable control protocol,
        # then intercept control_request/control_response via PermissionTransport
        options_kwargs = {
            "include_partial_messages": True,
            "cwd": workdir,
            "system_prompt": system_prompt or {"type": "preset", "preset": "claude_code"},
            "model": credentials.model,
            "max_turns": 100,
            "permission_prompt_tool_name": "stdio",  # Enable control protocol for permissions
            "setting_sources": ["user", "project", "local"],
            "extra_args": {"dangerously-skip-permissions": None}  # Enable bypassPermissions mode
        }

        # Add resume parameters if provided
        if resume_session_id:
            options_kwargs["resume"] = resume_session_id

        # Add resume-at parameter if provided (requires resume_session_id)
        if resume_at_message_uuid:
            if not resume_session_id:
                log.warning(f"resume_at_message_uuid provided without resume_session_id - ignoring")
            else:
                # Merge with existing extra_args (which has dangerously-skip-permissions)
                options_kwargs["extra_args"]["resume-session-at"] = resume_at_message_uuid

        try:
            # Inject PermissionTransport to handle control messages
            from .permission_transport import PermissionTransport
            import claude_agent_sdk._internal.transport.subprocess_cli as transport_module

            original_transport = transport_module.SubprocessCLITransport

            # Wrapper class that injects permission_manager and tab_id
            class PermissionTransportWrapper(PermissionTransport):
                def __init__(inner_self, *args, **kwargs):
                    log.info(f"PermissionTransportWrapper.__init__ called - injecting permission_manager and tab_id={tab_id}")
                    super().__init__(self.permission_manager, tab_id, *args, **kwargs)

            log.info(f"Monkey-patching SubprocessCLITransport with PermissionTransportWrapper")
            transport_module.SubprocessCLITransport = PermissionTransportWrapper

            try:
                options = ClaudeAgentOptions(**options_kwargs)

                mode_desc = "new session"
                if resume_session_id and resume_at_message_uuid:
                    mode_desc = f"resume at message (session={resume_session_id}, uuid={resume_at_message_uuid})"
                elif resume_session_id:
                    mode_desc = f"resume session (session={resume_session_id})"

                if os.getenv("KISUKE_DEBUG"):
                    log.info(f"Creating session {session_id} with permission handling ({mode_desc})")

                # Log that we're enabling bypassPermissions support
                log.info(f"Creating session {session_id} with --dangerously-skip-permissions flag (enables bypassPermissions mode)")

                client = ClaudeSDKClient(options)
                await client.connect()
            finally:
                # Restore original transport
                transport_module.SubprocessCLITransport = original_transport

            # Create session wrapper
            session = ClaudeSession(
                session_id=session_id,
                client=client,
                options=options,
                permission_handler=permission_handler
            )

            # Store session
            self.sessions[session_id] = session

            # Log appropriate message based on mode
            if resume_session_id and resume_at_message_uuid:
                log.info(f"Created Claude session {session_id} (resume at message: session={resume_session_id}, uuid={resume_at_message_uuid})")
            elif resume_session_id:
                log.info(f"Created Claude session {session_id} (resume from: {resume_session_id})")
            else:
                log.info(f"Created Claude session {session_id} (new)")
            return session

        except Exception as e:
            log.error(f"Failed to create Claude session: {e}")
            raise
    
    def get_session(self, session_id: str) -> Optional[ClaudeSession]:
        """Get session by ID."""
        return self.sessions.get(session_id)

    async def close_session(self, session_id: str):
        """
        Close a Claude session and clean up resources.

        Args:
            session_id: Session identifier to close
        """
        session = self.sessions.get(session_id)
        if not session:
            log.warning(f"Attempted to close non-existent session {session_id}")
            return

        try:
            # Close the client connection
            disconnect = getattr(session.client, "disconnect", None)
            if callable(disconnect):
                await disconnect()

            # Remove from sessions
            del self.sessions[session_id]
            log.info(f"Closed Claude session {session_id}")
            # Note: Proxy route cleanup is handled by SessionManager.destroy_session()

        except Exception as e:
            log.error(f"Error closing Claude session {session_id}: {e}")
    
    async def send_message(self, session_id: str, content: str, credentials, message_uuid: Optional[str] = None, response_callback: Optional[Callable] = None) -> bool:
        """
        Send a message to a Claude session with API credentials.
        Every message MUST have API credentials - no exceptions.

        Args:
            session_id: Session identifier
            content: Message content
            credentials: ClaudeApiCredentials to use for proxy communication (REQUIRED)
            message_uuid: Optional UUID for the user message (from iOS, for editing support)
            response_callback: Optional callback to stream response events

        Returns:
            True if sent successfully
        """
        # Validate credentials
        if not credentials or not hasattr(credentials, 'credential_id'):
            raise ValueError(f"Invalid credentials - every message must have valid API credentials")

        session = self.sessions.get(session_id)
        if not session:
            log.error(f"Session {session_id} not found")
            return False

        try:
            # Ensure proxy bridge token mirrors current credentials before sending.
            # This allows the Claude CLI (which authenticates with `kisuke-static`) to
            # reach the correct upstream route registered by the broker.
            from .routes import RouteManager
            try:
                # RouteManager is a singleton per broker; update bridge route on demand.
                route_manager = session.options.extra_args.get("route_manager") if isinstance(getattr(session.options, "extra_args", None), dict) else None
                if isinstance(route_manager, RouteManager):
                    route_manager.sync_bridge_route()
            except Exception as sync_exc:
                log.warning(f"Failed to sync bridge route before send: {sync_exc}")

            log.info(f"Sending message with credentials {credentials.credential_id} to session {session_id}, uuid={message_uuid}")

            if response_callback:
                # Use the new pattern: send and stream response
                await session.send_and_stream_response(content, response_callback, message_uuid=message_uuid)
            else:
                # Legacy: just send without streaming response
                await session.send(content, message_uuid=message_uuid)

            return True
        except Exception as e:
            log.error(f"Failed to send message: {e}")
            return False
    
    async def resolve_permission(
        self,
        session_id: str,
        request_id: str,
        decision: Dict[str, Any]
    ):
        """
        Resolve a permission request from iOS.

        Args:
            session_id: Session identifier
            request_id: Permission request ID
            decision: Permission decision from iOS (dict with 'behavior', etc.)
        """
        # Forward to permission manager to resolve pending request
        success = self.permission_manager.resolve_permission(request_id, decision)
        if success:
            log.info(f"Resolved permission {request_id} for session {session_id}: {decision.get('behavior')}")
        else:
            log.warning(f"No pending permission request found for {request_id}")
    
    async def close_session(self, session_id: str):
        """Close and remove a session."""
        session = self.sessions.pop(session_id, None)
        if session:
            try:
                # Claude SDK doesn't require explicit close
                log.info(f"Closed Claude session {session_id}")
            except Exception as e:
                log.error(f"Error closing session: {e}")
    
    async def close_all(self):
        """Close all active sessions."""
        session_ids = list(self.sessions.keys())
        for session_id in session_ids:
            await self.close_session(session_id)
        log.info("Closed all Claude sessions")
