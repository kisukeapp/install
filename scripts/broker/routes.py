"""
Route management for proxy configuration.
"""
import asyncio
import logging
import os
from typing import Dict, Any, Optional, List

# Import proxy helpers when available
try:
    from proxy.config import ModelConfig  # type: ignore
    from proxy.registry import (  # type: ignore
        register_route,
        clear_routes as proxy_clear_routes,
    )
except ImportError:  # pragma: no cover - used in isolated tests
    from dataclasses import dataclass, field

    @dataclass
    class ModelConfig:
        """Lightweight fallback for tests when proxy package is unavailable."""

        provider: str = "openai"
        base_url: str = "https://api.openai.com/v1"
        api_key: str = ""
        model: str = "gpt-4o"
        auth_method: Optional[str] = None
        extra_headers: Dict[str, str] = field(default_factory=dict)
        azure_deployment: Optional[str] = None
        azure_api_version: Optional[str] = None
        system_instruction: Optional[str] = None

    def register_route(token: str, cfg: ModelConfig) -> None:  # noqa: D401 - fallback stub
        """Mock proxy registration used only when proxy package is absent."""

    def unregister_route(token: str) -> None:  # noqa: D401 - fallback stub
        """Mock proxy unregistration used only when proxy package is absent."""

    def proxy_clear_routes() -> None:  # noqa: D401 - fallback stub
        """Mock proxy route clearing used only when proxy package is absent."""

log = logging.getLogger(__name__)
BRIDGE_ROUTE_TOKEN = os.getenv("KISUKE_BRIDGE_TOKEN", "kisuke-static")
from .utils import mask_secret

class RouteManager:
    """Manages proxy routes and their configuration."""
    
    def __init__(self):
        self.routes: Dict[str, ModelConfig] = {}
        self.active_token: Optional[str] = None
        self.stable_token: str = "kisuke-active"
        self._lock = asyncio.Lock()
        
    def register_routes(self, routes: List[Dict[str, Any]]) -> List[str]:
        """
        Register multiple routes from iOS client configuration.
        
        Args:
            routes: List of route configurations
            
        Returns:
            List of registered tokens
        """
        registered_tokens = []
        
        for route_data in routes:
            token = route_data.get("token")
            if not token:
                log.warning("Route missing token, skipping")
                continue
            
            try:
                config = self._create_model_config(route_data)
                self.routes[token] = config
                registered_tokens.append(token)
                
                # Register with proxy if available
                try:
                    register_route(token, config)
                    log.info(
                        "Registered route token=%s provider=%s model=%s base_url=%s auth=%s key=%s",
                        token,
                        config.provider,
                        config.model,
                        config.base_url,
                        config.auth_method,
                        mask_secret(config.api_key),
                    )
                except Exception as e:
                    log.error(f"Failed to register route with proxy: {e}")
                
                if token == self.active_token:
                    self._sync_bridge_route(config)
            except Exception as e:
                log.error(f"Failed to register route {token}: {e}")
        
        # Set first route as active if none set
        if registered_tokens and not self.active_token:
            self.active_token = registered_tokens[0]
            log.info(f"Set active route to {self.active_token}")
            self._sync_bridge_route(self.routes[self.active_token])
        
        return registered_tokens
    
    async def set_active_route(self, token: str) -> bool:
        """
        Set the active route token (thread-safe).
        
        Args:
            token: Route token to activate
            
        Returns:
            True if successful, False if token not found
        """
        async with self._lock:
            if token not in self.routes:
                log.error(f"Cannot set active route - token not found: {token}")
                return False
            
            old_token = self.active_token
            self.active_token = token
            log.info(f"Active route changed from {old_token} to {token}")
            self._sync_bridge_route(self.routes[token])
            return True
    
    async def set_stable_route(self, token: str) -> bool:
        """
        Set the stable route token (thread-safe).
        
        Args:
            token: Route token to set as stable
            
        Returns:
            True if successful, False if token not found
        """
        async with self._lock:
            if token not in self.routes:
                log.error(f"Cannot set stable route - token not found: {token}")
                return False
            
            old_token = self.stable_token
            self.stable_token = token
            log.info(f"Stable route changed from {old_token} to {token}")
            return True
    
    def get_active_route(self) -> Optional[str]:
        """Get the currently active route token."""
        return self.active_token
    
    def get_route_config(self, token: str) -> Optional[ModelConfig]:
        """Get configuration for a specific route."""
        return self.routes.get(token)
    
    def _create_model_config(self, route_data: Dict[str, Any]) -> ModelConfig:
        """Create ModelConfig from route payload provided by iOS."""
        config_data = route_data.get("config", route_data)

        api_key = config_data.get("api_key", "")
        if not api_key:
            raise ValueError("Route configuration missing api_key")

        return ModelConfig(
            provider=config_data.get("provider", "openai"),
            base_url=config_data.get("base_url", "https://api.openai.com/v1"),
            api_key=api_key,
            model=config_data.get("model", "gpt-4o"),
            auth_method=config_data.get("auth_method"),
            extra_headers=config_data.get("extra_headers", {}),
            azure_deployment=config_data.get("azure_deployment"),
            azure_api_version=config_data.get("azure_api_version"),
            system_instruction=config_data.get("system_instruction"),
        )
    
    def get_api_key_for_session(self) -> str:
        """Get API key for current active route."""
        if self.active_token:
            return self.active_token
        return self.stable_token
    
    def clear_routes(self):
        """Clear all registered routes."""
        self.routes.clear()
        self.active_token = None
        try:
            proxy_clear_routes()
        except Exception as exc:  # pragma: no cover - logging only
            log.warning(f"Failed to clear proxy routes: {exc}")
        log.info("Cleared all routes")

    def _sync_bridge_route(self, source_config: ModelConfig) -> None:
        """Ensure the bridge token points to the currently active config."""
        if not source_config or BRIDGE_ROUTE_TOKEN == self.active_token:
            return

        bridge_config = self._clone_model_config(source_config)
        self.routes[BRIDGE_ROUTE_TOKEN] = bridge_config

        try:
            register_route(BRIDGE_ROUTE_TOKEN, bridge_config)
            log.info(
                "Updated bridge route token=%s provider=%s model=%s base_url=%s auth=%s",
                BRIDGE_ROUTE_TOKEN,
                bridge_config.provider,
                bridge_config.model,
                bridge_config.base_url,
                bridge_config.auth_method,
            )
        except Exception as exc:
            log.error(f"Failed to register bridge route: {exc}")

    def sync_bridge_route(self) -> None:
        """Public wrapper to resync the bridge token with current active route."""
        if self.active_token and self.active_token in self.routes:
            self._sync_bridge_route(self.routes[self.active_token])

    @staticmethod
    def _clone_model_config(config: ModelConfig) -> ModelConfig:
        """Deep-ish copy of ModelConfig to avoid shared mutations."""
        return ModelConfig(
            provider=config.provider,
            base_url=config.base_url,
            api_key=config.api_key,
            model=config.model,
            auth_method=config.auth_method,
            extra_headers=dict(config.extra_headers or {}),
            azure_deployment=config.azure_deployment,
            azure_api_version=config.azure_api_version,
            system_instruction=config.system_instruction,
        )
    
    # Properties expected by handlers.py
    @property
    def active_route_token(self) -> Optional[str]:
        """Get the currently active route token."""
        return self.active_token
    
    @property 
    def stable_route_token(self) -> str:
        """Get the stable route token."""
        return self.stable_token
    
    # Methods expected by handlers.py
    def get_route(self, token: str) -> Optional[ModelConfig]:
        """Get route configuration by token (alias for get_route_config)."""
        return self.get_route_config(token)
    
    def get_all_routes(self) -> Dict[str, Dict[str, Any]]:
        """Get all routes in a serializable format."""
        result = {}
        for token, config in self.routes.items():
            if token == BRIDGE_ROUTE_TOKEN:
                continue
            result[token] = {
                "token": token,
                "config": self._model_config_to_dict(config),
            }
        return result
    
    def _model_config_to_dict(self, model_config: ModelConfig) -> Dict[str, Any]:
        """Convert ModelConfig to dict for serialization."""
        return {
            "provider": model_config.provider,
            "base_url": model_config.base_url,
            "api_key": model_config.api_key,
            "model": model_config.model,
            "auth_method": model_config.auth_method,
            "extra_headers": model_config.extra_headers,
            "azure_deployment": model_config.azure_deployment,
            "azure_api_version": model_config.azure_api_version,
            "system_instruction": model_config.system_instruction,
        }
