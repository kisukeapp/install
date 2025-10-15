"""
Utility functions for Kisuke Broker.
"""
import os
import sys
import json
import uuid
import logging
import subprocess
from pathlib import Path
from typing import Optional, Any, Dict, List
from functools import lru_cache
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone

log = logging.getLogger(__name__)

try:  # Imported lazily to support environments where websockets may differ
    from websockets.protocol import State as _WebSocketState  # type: ignore
except Exception:  # pragma: no cover - websockets might not be available during tooling
    _WebSocketState = None

class PathUtils:
    """Utilities for managing paths with proper expansion."""
    
    @staticmethod
    def expand_path(path_str: str) -> Path:
        """
        Expands a path string with support for:
        - Home directory (~)
        - Environment variables ($VAR or ${VAR})
        - Relative paths (resolved to absolute)
        
        Args:
            path_str: Path string to expand
            
        Returns:
            Path: Expanded Path object
        """
        if not path_str:
            return Path.cwd()
        
        # First expand environment variables
        expanded = os.path.expandvars(path_str)
        
        # Then expand home directory
        expanded = os.path.expanduser(expanded)
        
        # Convert to Path and resolve to absolute
        path = Path(expanded).resolve()
        
        return path
    
    @staticmethod
    def ensure_directory(path: Path) -> Path:
        """
        Ensures a directory exists, creating it if necessary.
        
        Args:
            path: Directory path
            
        Returns:
            Path: The directory path
            
        Raises:
            PermissionError: If directory cannot be created
        """
        try:
            path.mkdir(parents=True, exist_ok=True)
            return path
        except PermissionError as e:
            log.error(f"Cannot create directory {path}: {e}")
            raise
        except Exception as e:
            log.error(f"Error creating directory {path}: {e}")
            raise
    
    @staticmethod
    def validate_workdir(workdir: str) -> Path:
        """
        Validates and prepares a working directory.
        
        Args:
            workdir: Working directory path string
            
        Returns:
            Path: Validated and expanded Path object
            
        Raises:
            ValueError: If path is invalid or cannot be accessed
        """
        try:
            path = PathUtils.expand_path(workdir)
            
            # Ensure it exists and is a directory
            if not path.exists():
                PathUtils.ensure_directory(path)
            elif not path.is_dir():
                raise ValueError(f"Path exists but is not a directory: {path}")
            
            # Check if we can write to it
            test_file = path / f".kisuke_test_{uuid.uuid4().hex[:8]}"
            try:
                test_file.touch()
                test_file.unlink()
            except Exception as e:
                raise ValueError(f"Cannot write to directory {path}: {e}")
            
            return path
            
        except Exception as e:
            log.error(f"Invalid working directory '{workdir}': {e}")
            raise ValueError(f"Invalid working directory: {e}")

def generate_id() -> str:
    """Generate a unique ID."""
    return str(uuid.uuid4())

def generate_short_id() -> str:
    """Generate a short unique ID."""
    return uuid.uuid4().hex[:8]

def mask_secret(s: Optional[str]) -> str:
    """Mask sensitive information for logging."""
    if not s:
        return ""
    if len(s) <= 8:
        return "****"
    return f"{s[:4]}...{s[-4:]}"

def format_json(data: Any, indent: int = 2) -> str:
    """Format data as JSON string."""
    try:
        return json.dumps(data, indent=indent, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(data)


def websocket_is_open(ws: Any) -> bool:
    """Best-effort detection of websocket open state across library versions."""
    if ws is None:
        return False

    open_attr = getattr(ws, "open", None)
    if isinstance(open_attr, bool):
        return open_attr
    if callable(open_attr):
        try:
            result = open_attr()
            if isinstance(result, bool):
                return result
        except Exception:
            pass

    state = getattr(ws, "state", None)
    if state is not None:
        if _WebSocketState is not None:
            try:
                if state is _WebSocketState.OPEN:
                    return True
            except Exception:
                pass
        if getattr(state, "name", "").upper() == "OPEN":
            return True
        if getattr(state, "name", "").upper() == "CLOSED":
            return False

    closed_attr = getattr(ws, "closed", None)
    if isinstance(closed_attr, bool):
        return not closed_attr

    close_code = getattr(ws, "close_code", None)
    if close_code is not None:
        return False

    return True


def dataclass_to_dict(value: Any) -> Any:
    """Recursively convert dataclass instances into plain Python structures."""
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, dict):
        return {k: dataclass_to_dict(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [dataclass_to_dict(v) for v in value]
    return value

@lru_cache(maxsize=1)
def get_claude_cli_path() -> Optional[str]:
    """Find the Claude CLI executable path.

    Search order:
    1) Kisuke isolated paths
    2) PATH
    3) Common system locations
    4) npm prefix (Kisuke wrapper first, then system npm)
    """
    # 1) Kisuke isolated installs
    kisuke_paths = [
        os.path.expanduser("~/.kisuke/bin/claude"),
        os.path.expanduser("~/.kisuke/bin/nodejs/bin/claude"),
    ]
    for p in kisuke_paths:
        if os.path.exists(p) and os.access(p, os.X_OK):
            return p

    # 2) PATH
    try:
        result = subprocess.run(["which", "claude"], capture_output=True, text=True)
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass

    # 3) Common system locations
    common_paths = [
        "/usr/local/bin/claude",
        "/opt/homebrew/bin/claude",
        os.path.expanduser("~/.local/bin/claude"),
        os.path.expanduser("~/bin/claude"),
    ]
    for path in common_paths:
        if os.path.exists(path) and os.access(path, os.X_OK):
            return path

    # 4) npm prefix discovery (prefer Kisuke wrapper)
    try:
        # Try Kisuke npm wrapper first
        kisuke_npm = os.path.expanduser("~/.kisuke/bin/npm")
        prefixes_to_try: List[str] = []
        if os.path.exists(kisuke_npm) and os.access(kisuke_npm, os.X_OK):
            pfx = subprocess.run([kisuke_npm, "config", "get", "prefix"], capture_output=True, text=True)
            if pfx.returncode == 0:
                prefixes_to_try.append(pfx.stdout.strip())
        # Fallback to system npm
        pfx2 = subprocess.run(["npm", "config", "get", "prefix"], capture_output=True, text=True)
        if pfx2.returncode == 0:
            prefixes_to_try.append(pfx2.stdout.strip())

        for prefix in prefixes_to_try:
            if not prefix:
                continue
            path = os.path.join(prefix, "bin", "claude")
            if os.path.exists(path) and os.access(path, os.X_OK):
                return path
    except Exception:
        pass

    return None

def setup_logging(level: str = "INFO") -> None:
    """Set up logging configuration."""
    from .config import LOG_FORMAT, LOG_DATE_FORMAT

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format=LOG_FORMAT,
        datefmt=LOG_DATE_FORMAT
    )

    # Filter out websockets handshake errors (port scanners, health checks)
    class WebSocketHandshakeFilter(logging.Filter):
        """Suppress EOFError handshake failures (harmless - port scanners/health checks)."""
        def filter(self, record):
            # Suppress "opening handshake failed" + EOFError messages
            if "opening handshake failed" in record.getMessage():
                return False
            if "EOFError" in record.getMessage() and "handshake" in record.getMessage().lower():
                return False
            return True

    websockets_logger = logging.getLogger('websockets.server')
    websockets_logger.addFilter(WebSocketHandshakeFilter())


# Conversation history utilities

def sanitize_project_path(cwd: str) -> str:
    """
    Sanitize a project path for Claude CLI storage format.

    Converts "/Users/foo/bar" to "-Users-foo-bar"

    Args:
        cwd: Working directory path

    Returns:
        Sanitized path string
    """
    return cwd.replace('/', '-')


def get_last_user_message(filepath: str, rg_path: str) -> Optional[str]:
    """
    Extract the last user message from a conversation file using rg.

    Args:
        filepath: Path to .jsonl conversation file
        rg_path: Path to ripgrep binary

    Returns:
        Last user message content or None if not found
    """
    try:
        if rg_path and os.path.exists(rg_path):
            # Use rg for blazing fast search
            result = subprocess.run(
                [rg_path, '"type":"user"', filepath, '-N'],
                capture_output=True,
                text=True,
                timeout=5
            )

            if result.returncode == 0 and result.stdout:
                # Search backwards through user messages to find actual text
                lines = result.stdout.strip().split('\n')
                for line in reversed(lines):
                    data = json.loads(line)
                    message = data.get('message', {})
                    content = message.get('content', '')

                    # Handle both string and array content formats
                    if isinstance(content, list):
                        # Extract text from array format (skip tool_result)
                        text_parts = [item.get('text', '') for item in content if isinstance(item, dict) and item.get('type') == 'text']
                        text = ' '.join(text_parts) if text_parts else ''
                        if text:  # Only return if we found actual text
                            return text
                    elif content:  # String content
                        return content

        # Fallback: read file backwards to find last user message
        with open(filepath, 'r') as f:
            lines = f.readlines()

        # Search from end backwards for actual user text
        for line in reversed(lines):
            try:
                data = json.loads(line.strip())
                if data.get('type') == 'user':
                    message = data.get('message', {})
                    content = message.get('content', '')

                    # Handle both formats
                    if isinstance(content, list):
                        # Extract text from array format (skip tool_result)
                        text_parts = [item.get('text', '') for item in content if isinstance(item, dict) and item.get('type') == 'text']
                        text = ' '.join(text_parts) if text_parts else ''
                        if text:  # Only return if we found actual text
                            return text
                    elif content:  # String content
                        return content
            except json.JSONDecodeError:
                continue

    except Exception as e:
        log.warning(f"Failed to extract last user message from {filepath}: {e}")

    return None


def list_conversations_for_project(cwd: str, projects_dir: Optional[str] = None, rg_path: Optional[str] = None) -> list:
    """
    List all conversations for a specific project.

    Args:
        cwd: Project working directory path
        projects_dir: Path to Claude projects directory (uses config default if None)
        rg_path: Path to ripgrep binary (uses config default if None)

    Returns:
        List of conversation metadata dicts, sorted by most recent first
    """
    from .config import CLAUDE_PROJECTS_DIR, RG_PATH

    projects_dir = projects_dir or CLAUDE_PROJECTS_DIR
    rg_path = rg_path or RG_PATH

    sanitized = sanitize_project_path(cwd)
    project_dir = Path(os.path.expanduser(projects_dir)) / sanitized

    if not project_dir.exists():
        log.debug(f"No conversation history found for project: {cwd}")
        return []

    conversations = []

    try:
        # Use scandir for better performance
        with os.scandir(project_dir) as entries:
            for entry in entries:
                if not entry.name.endswith('.jsonl') or not entry.is_file():
                    continue

                try:
                    filepath = Path(entry.path)

                    # Get mtime early for caching
                    mtime = entry.stat().st_mtime

                    # Read first line for metadata
                    with open(filepath, 'r') as f:
                        first_line = f.readline().strip()
                        if not first_line:
                            continue

                        data = json.loads(first_line)

                        # Extract metadata
                        session_id = data.get('sessionId') or filepath.stem
                        # Convert mtime to ISO 8601 format to match JSONL format
                        timestamp = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat().replace('+00:00', 'Z')
                        conv_cwd = data.get('cwd', cwd)
                        git_branch = data.get('gitBranch')

                        # Get last user message
                        last_user_message = get_last_user_message(str(filepath), rg_path)
                        if not last_user_message:
                            # Fallback to first message if no user message found
                            message = data.get('message', {})
                            content = message.get('content', '')
                            if isinstance(content, list):
                                text_parts = [item.get('text', '') for item in content if isinstance(item, dict) and item.get('type') == 'text']
                                last_user_message = ' '.join(text_parts) if text_parts else '(empty conversation)'
                            else:
                                last_user_message = content or '(empty conversation)'

                        conversations.append({
                            'sessionId': session_id,
                            'timestamp': timestamp,
                            'cwd': conv_cwd,
                            'gitBranch': git_branch,
                            'lastUserMessage': last_user_message[:200],  # Limit preview length
                            '_mtime': mtime  # Cache for efficient sorting
                        })

                except (json.JSONDecodeError, OSError) as e:
                    log.warning(f"Failed to parse conversation file {entry.path}: {e}")
                    continue

        # Sort by cached mtime (most recent first)
        conversations.sort(key=lambda c: c['_mtime'], reverse=True)

        # Remove internal _mtime field before returning
        for conv in conversations:
            conv.pop('_mtime', None)

    except Exception as e:
        log.error(f"Error listing conversations for {cwd}: {e}")

    return conversations


def list_all_conversations(projects_dir: Optional[str] = None, rg_path: Optional[str] = None) -> list:
    """
    List all conversations from all projects.

    Args:
        projects_dir: Path to Claude projects directory (uses config default if None)
        rg_path: Path to ripgrep binary (uses config default if None)

    Returns:
        List of conversation metadata dicts, sorted by most recent first
    """
    from .config import CLAUDE_PROJECTS_DIR, RG_PATH

    projects_dir = projects_dir or CLAUDE_PROJECTS_DIR
    rg_path = rg_path or RG_PATH

    projects_path = Path(os.path.expanduser(projects_dir))
    if not projects_path.exists():
        log.info(f"Claude projects directory not found: {projects_path}")
        return []

    all_conversations = []

    try:
        # Iterate through all project directories
        for project_dir in projects_path.iterdir():
            if not project_dir.is_dir():
                continue

            # Extract cwd from sanitized directory name
            cwd = project_dir.name.replace('-', '/', 1)  # First dash becomes /

            # Get conversations for this project (already sorted, with _mtime cached)
            # Temporarily get conversations with _mtime for global sorting
            sanitized = sanitize_project_path(cwd)
            project_path = projects_path / sanitized

            if not project_path.exists():
                continue

            with os.scandir(project_path) as entries:
                for entry in entries:
                    if not entry.name.endswith('.jsonl') or not entry.is_file():
                        continue

                    try:
                        filepath = Path(entry.path)
                        mtime = entry.stat().st_mtime

                        # Read first line for metadata
                        with open(filepath, 'r') as f:
                            first_line = f.readline().strip()
                            if not first_line:
                                continue

                            data = json.loads(first_line)

                            # Extract metadata
                            session_id = data.get('sessionId') or filepath.stem
                            timestamp = data.get('timestamp', '')
                            conv_cwd = data.get('cwd', cwd)
                            git_branch = data.get('gitBranch')

                            # Get last user message
                            last_user_message = get_last_user_message(str(filepath), rg_path)
                            if not last_user_message:
                                message = data.get('message', {})
                                content = message.get('content', '')
                                if isinstance(content, list):
                                    text_parts = [item.get('text', '') for item in content if isinstance(item, dict) and item.get('type') == 'text']
                                    last_user_message = ' '.join(text_parts) if text_parts else '(empty conversation)'
                                else:
                                    last_user_message = content or '(empty conversation)'

                            all_conversations.append({
                                'sessionId': session_id,
                                'timestamp': timestamp,
                                'cwd': conv_cwd,
                                'gitBranch': git_branch,
                                'lastUserMessage': last_user_message[:200],
                                '_mtime': mtime
                            })

                    except (json.JSONDecodeError, OSError) as e:
                        log.warning(f"Failed to parse conversation file {entry.path}: {e}")
                        continue

        # Sort all conversations by cached mtime (most recent first)
        all_conversations.sort(key=lambda c: c['_mtime'], reverse=True)

        # Remove internal _mtime field before returning
        for conv in all_conversations:
            conv.pop('_mtime', None)

    except Exception as e:
        log.error(f"Error listing all conversations: {e}")

    return all_conversations


def load_conversation_events(
    cwd: str,
    session_id: str,
    from_second_to_last_user: bool = True
) -> List[dict]:
    """
    Load conversation events from .jsonl file using ripgrep for speed.

    For large conversations (50MB+), this loads from second-to-last user message onwards
    to avoid overwhelming iOS with full history.

    Strategy:
    1. Use ripgrep with -n to find line numbers of all external user messages
    2. Get second-to-last user message line number
    3. Use tail to extract from that line onwards
    4. Parse JSON events

    Args:
        cwd: Project working directory
        session_id: Session ID (conversation file name without .jsonl)
        from_second_to_last_user: If True, load from 2nd-to-last user message (default)

    Returns:
        List of parsed JSON event dicts
    """
    from .config import CLAUDE_PROJECTS_DIR, RG_PATH

    # Build file path
    sanitized = sanitize_project_path(cwd)
    project_dir = Path(os.path.expanduser(CLAUDE_PROJECTS_DIR)) / sanitized
    conversation_file = project_dir / f"{session_id}.jsonl"

    if not conversation_file.exists():
        log.warning(f"Conversation file not found: {conversation_file}")
        return []

    try:
        start_line = 1  # Default: load entire file

        if from_second_to_last_user:
            # Find second-to-last external user message line number
            start_line = _find_second_to_last_user_line(str(conversation_file), RG_PATH)

        # Extract events from start_line onwards using tail
        events = _extract_events_from_line(str(conversation_file), start_line)

        log.info(f"Loaded {len(events)} events from conversation {session_id} (from line {start_line})")
        return events

    except Exception as e:
        log.error(f"Failed to load conversation {session_id}: {e}")
        return []


def _find_second_to_last_user_line(filepath: str, rg_path: str) -> int:
    """
    Find the line number of the second-to-last external user message.

    Uses ripgrep with -n flag to get line numbers.

    Args:
        filepath: Path to .jsonl file
        rg_path: Path to ripgrep binary

    Returns:
        Line number (1-indexed) or 1 if not found
    """
    try:
        rg_path_expanded = os.path.expanduser(rg_path)

        if not os.path.exists(rg_path_expanded):
            log.debug(f"ripgrep not found at {rg_path_expanded}, loading full conversation")
            return 1

        # Find all lines with external user messages
        # Look for: "type":"user" AND "userType":"external"
        result = subprocess.run(
            [
                rg_path_expanded,
                '"type":"user".*"userType":"external"',
                filepath,
                '-n',  # Show line numbers
                '--no-heading'
            ],
            capture_output=True,
            text=True,
            timeout=10
        )

        if result.returncode != 0 or not result.stdout:
            log.debug("No external user messages found, loading full conversation")
            return 1

        # Parse line numbers from output (format: "123:{json...}")
        lines = result.stdout.strip().split('\n')
        line_numbers = []

        for line in lines:
            try:
                line_num_str = line.split(':', 1)[0]
                line_numbers.append(int(line_num_str))
            except (ValueError, IndexError):
                continue

        if len(line_numbers) < 2:
            # Less than 2 user messages, load from beginning
            log.debug(f"Only {len(line_numbers)} user messages, loading full conversation")
            return 1

        # Return second-to-last user message line number
        second_to_last = line_numbers[-2]
        log.debug(f"Found {len(line_numbers)} user messages, loading from line {second_to_last}")
        return second_to_last

    except subprocess.TimeoutExpired:
        log.warning(f"Timeout finding user messages in {filepath}, loading full conversation")
        return 1
    except Exception as e:
        log.warning(f"Error finding user messages with rg: {e}, loading full conversation")
        return 1


def _extract_events_from_line(filepath: str, start_line: int) -> List[dict]:
    """
    Extract and parse JSON events from a specific line onwards.

    Uses tail for efficiency on large files.

    Args:
        filepath: Path to .jsonl file
        start_line: Starting line number (1-indexed)

    Returns:
        List of parsed JSON event dicts
    """
    events = []

    try:
        # Use tail to efficiently read from start_line onwards
        # tail -n +N reads from line N to end
        result = subprocess.run(
            ['tail', f'-n', f'+{start_line}', filepath],
            capture_output=True,
            text=True,
            timeout=30
        )

        if result.returncode != 0:
            log.error(f"tail command failed: {result.stderr}")
            return []

        # Parse each line as JSON
        for line_num, line in enumerate(result.stdout.split('\n'), start=start_line):
            line = line.strip()
            if not line:
                continue

            try:
                event = json.loads(line)
                events.append(event)
            except json.JSONDecodeError as e:
                log.debug(f"Failed to parse JSON at line {line_num}: {e}")
                continue

        return events

    except subprocess.TimeoutExpired:
        log.error(f"Timeout extracting events from {filepath}")
        return []
    except Exception as e:
        log.error(f"Error extracting events: {e}")
        return []
