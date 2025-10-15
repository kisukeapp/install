"""
Runtime-modifiable permission manager for tool execution control.

This module provides a permission manager with mutable state that can be
modified at runtime to change permission behavior dynamically.
"""
import asyncio
import logging
import time
from typing import Dict, Any, Optional, Callable, Awaitable
from dataclasses import dataclass, field
from enum import Enum

log = logging.getLogger(__name__)


class PermissionMode(str, Enum):
    """Permission modes for tool execution."""
    ALLOW = "allow"          # Allow all tools
    DENY = "deny"            # Deny all tools
    PROMPT = "prompt"        # Prompt iOS for each tool
    CACHED = "cached"        # Use cached decisions
    CUSTOM = "custom"        # Use custom rules


@dataclass
class PermissionDecision:
    """A permission decision for a tool."""
    behavior: str  # "allow", "deny", or "escalate"
    updated_input: Optional[Dict[str, Any]] = None
    reason: Optional[str] = None
    timestamp: float = field(default_factory=time.time)


@dataclass
class PendingPermissionRequest:
    """A pending permission request awaiting iOS response."""
    request_id: str
    tool_name: str
    tool_input: Dict[str, Any]
    future: asyncio.Future
    timestamp: float = field(default_factory=time.time)
    timeout: float = 30.0  # 30 seconds default timeout


class RuntimePermissionManager:
    """
    Manager for runtime-modifiable tool permissions.
    
    This manager maintains mutable state that can be changed at runtime,
    allowing dynamic modification of permission behavior without restarting
    sessions or recreating clients.
    """
    
    def __init__(self, initial_mode: PermissionMode = PermissionMode.PROMPT):
        """
        Initialize the permission manager.
        
        Args:
            initial_mode: Initial permission mode
        """
        # Runtime-modifiable state
        self.permission_mode = initial_mode
        self.ios_handler: Optional[Callable] = None
        
        # Permission rules and cache
        self.permission_rules: Dict[str, str] = {}  # tool_name -> behavior
        self.permission_cache: Dict[str, PermissionDecision] = {}
        self.cache_ttl = 300  # 5 minutes cache TTL
        
        # Pending requests
        self.pending_requests: Dict[str, PendingPermissionRequest] = {}
        
        # Statistics
        self.stats = {
            "total_requests": 0,
            "allowed": 0,
            "denied": 0,
            "escalated": 0,
            "timed_out": 0
        }
    
    def set_mode(self, mode: str) -> None:
        """
        Change permission mode at runtime.
        
        Args:
            mode: New permission mode
        """
        old_mode = self.permission_mode
        try:
            self.permission_mode = PermissionMode(mode)
            log.info(f"Permission mode changed: {old_mode} -> {self.permission_mode}")
        except ValueError:
            log.error(f"Invalid permission mode: {mode}")
    
    def set_ios_handler(self, handler: Optional[Callable]) -> None:
        """
        Update iOS permission handler at runtime.
        
        Args:
            handler: Async callable for iOS permission requests
        """
        self.ios_handler = handler
        log.info(f"iOS handler {'set' if handler else 'cleared'}")
    
    def update_permission_rules(self, rules: Dict[str, str]) -> None:
        """
        Update permission rules at runtime.
        
        Args:
            rules: Dictionary mapping tool names to behaviors
        """
        self.permission_rules.update(rules)
        log.info(f"Updated {len(rules)} permission rules")
    
    def clear_cache(self) -> None:
        """Clear the permission cache."""
        self.permission_cache.clear()
        log.info("Permission cache cleared")
    
    async def get_permission(
        self,
        tool_name: str,
        tool_input: Dict[str, Any],
        request_id: str
    ) -> Dict[str, Any]:
        """
        Get permission decision for a tool, consulting current runtime state.
        
        Args:
            tool_name: Name of the tool
            tool_input: Tool input parameters
            request_id: Request identifier
            
        Returns:
            Permission decision dictionary with behavior and optional updated input
        """
        self.stats["total_requests"] += 1
        
        # Check current mode (can change at runtime!)
        if self.permission_mode == PermissionMode.ALLOW:
            self.stats["allowed"] += 1
            decision = {
                "behavior": "allow",
                "updatedInput": tool_input
            }
            log.debug(f"Permission decision for {tool_name}: {decision}")
            return decision
        
        elif self.permission_mode == PermissionMode.DENY:
            self.stats["denied"] += 1
            return {
                "behavior": "deny",
                "reason": "All tools denied by current mode"
            }
        
        elif self.permission_mode == PermissionMode.CACHED:
            # Check cache first
            cache_key = f"{tool_name}:{hash(str(sorted(tool_input.items())))}"
            cached = self.permission_cache.get(cache_key)
            
            if cached and (time.time() - cached.timestamp) < self.cache_ttl:
                log.debug(f"Using cached permission for {tool_name}")
                return {
                    "behavior": cached.behavior,
                    "updatedInput": cached.updated_input or tool_input
                }
        
        elif self.permission_mode == PermissionMode.CUSTOM:
            # Check custom rules
            if tool_name in self.permission_rules:
                behavior = self.permission_rules[tool_name]
                if behavior == "allow":
                    self.stats["allowed"] += 1
                else:
                    self.stats["denied"] += 1
                
                return {
                    "behavior": behavior,
                    "updatedInput": tool_input if behavior == "allow" else None
                }
        
        # Default to PROMPT mode - forward to iOS
        if self.permission_mode == PermissionMode.PROMPT or not self.permission_rules.get(tool_name):
            return await self._request_ios_permission(tool_name, tool_input, request_id)
        
        # Default deny if no handler
        self.stats["denied"] += 1
        return {
            "behavior": "deny",
            "reason": "No permission handler available"
        }
    
    async def _request_ios_permission(
        self,
        tool_name: str,
        tool_input: Dict[str, Any],
        request_id: str
    ) -> Dict[str, Any]:
        """
        Request permission from iOS client.
        
        Args:
            tool_name: Name of the tool
            tool_input: Tool input parameters
            request_id: Request identifier
            
        Returns:
            Permission decision from iOS
        """
        if not self.ios_handler:
            log.warning(f"No iOS handler for permission request: {tool_name}")
            self.stats["denied"] += 1
            return {
                "behavior": "deny",
                "reason": "No iOS handler configured"
            }
        
        # Create pending request with future
        future = asyncio.Future()
        pending = PendingPermissionRequest(
            request_id=request_id,
            tool_name=tool_name,
            tool_input=tool_input,
            future=future
        )
        
        self.pending_requests[request_id] = pending

        try:
            # Call iOS handler (non-blocking)
            asyncio.create_task(self.ios_handler(tool_name, tool_input, request_id))

            # Wait for response indefinitely (no timeout - iOS always responds or sends interrupt)
            decision = await future

            log.info(f"Received decision from future for {tool_name}: {decision}")

            # Update stats
            self.stats["escalated"] += 1
            if decision.get("behavior") == "allow":
                self.stats["allowed"] += 1
            else:
                self.stats["denied"] += 1

            # Cache the decision
            if self.permission_mode == PermissionMode.CACHED:
                cache_key = f"{tool_name}:{hash(str(sorted(tool_input.items())))}"
                self.permission_cache[cache_key] = PermissionDecision(
                    behavior=decision.get("behavior", "deny"),
                    updated_input=decision.get("updatedInput")
                )

            log.info(f"Returning decision from _request_ios_permission: {decision}")
            return decision
        finally:
            # Clean up pending request
            self.pending_requests.pop(request_id, None)
    
    def resolve_permission(self, request_id: str, decision: Dict[str, Any]) -> bool:
        """
        Resolve a pending permission request with iOS response.

        Args:
            request_id: Request identifier
            decision: Permission decision from iOS (behavior="allow" or "deny")
                Note: "auto" is handled at the handler level and converted to "allow"

        Returns:
            True if request was found and resolved, False otherwise
        """
        log.info(f"resolve_permission called: request_id={request_id}, decision={decision}")

        pending = self.pending_requests.get(request_id)
        if not pending:
            log.warning(f"No pending request found for {request_id}")
            return False

        if not pending.future.done():
            # Ensure updatedInput is present for allow decisions
            if decision.get("behavior") == "allow" and "updatedInput" not in decision:
                log.info(f"Adding updatedInput to decision: tool_input={pending.tool_input}")
                decision["updatedInput"] = pending.tool_input

            log.info(f"Setting future result to: {decision}")
            pending.future.set_result(decision)
            log.info(f"Resolved permission request {request_id}: {decision.get('behavior')}")
            return True

        return False
    
    def get_stats(self) -> Dict[str, Any]:
        """
        Get permission manager statistics.
        
        Returns:
            Dictionary with statistics
        """
        return {
            "mode": self.permission_mode.value,
            "has_ios_handler": self.ios_handler is not None,
            "pending_requests": len(self.pending_requests),
            "cached_decisions": len(self.permission_cache),
            "custom_rules": len(self.permission_rules),
            **self.stats
        }
    
    def reset_stats(self) -> None:
        """Reset statistics counters."""
        self.stats = {
            "total_requests": 0,
            "allowed": 0,
            "denied": 0,
            "escalated": 0,
            "timed_out": 0
        }