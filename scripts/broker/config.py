"""
Configuration constants and settings for Kisuke Broker.
"""
import os
from typing import Optional

# WebSocket Configuration
PORT = int(os.getenv("BROKER_PORT", "8765"))
HOST = "0.0.0.0"

# Proxy Configuration
PROXY_HOST = os.getenv("PROXY_HOST", "127.0.0.1")
PROXY_PORT = int(os.getenv("PROXY_PORT", "8082"))

# Session Configuration
SESSION_TIMEOUT = float(os.getenv("SESSION_TIMEOUT", "1800.0"))  # 30 minutes default
SESSION_CLEANUP_INTERVAL = 60.0  # 1 minute
INACTIVE_THRESHOLD = 10.0  # 10 seconds

# Queue Configuration
QUEUE_WAIT_TIMEOUT = 600  # 10 minutes max wait for queue items
ACK_TIMEOUT = 5.0  # Timeout for iOS acknowledgments
RESPONSE_RETRY_DELAY = 3.0  # Delay before retrying unacknowledged responses
MAX_RETRY_ATTEMPTS = 3

# Claude Configuration
DEFAULT_ANTHROPIC_BASE_URL = f"http://{PROXY_HOST}:{PROXY_PORT}"
DEFAULT_ANTHROPIC_API_KEY = "kisuke-static"

# Logging Configuration
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_FORMAT = '%(asctime)s | %(levelname)-8s | %(message)s'
LOG_DATE_FORMAT = '%Y-%m-%d %H:%M:%S'

# Permission Configuration
DEFAULT_PERMISSION_MODE = "allow"  # "allow", "deny", "prompt"

# Conversation History Configuration
CLAUDE_PROJECTS_DIR = "~/.claude/projects"
RG_PATH = "~/.kisuke/bin/rg"

def get_anthropic_env(api_key: Optional[str] = None) -> dict:
    """Get environment variables for Anthropic SDK."""
    env = dict(os.environ)
    env["ANTHROPIC_BASE_URL"] = DEFAULT_ANTHROPIC_BASE_URL
    if api_key:
        env["ANTHROPIC_API_KEY"] = api_key
    return env
