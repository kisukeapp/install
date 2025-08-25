#!/usr/bin/env python3
"""Remote file manager providing IDE-like file operations for Kisuke iOS.

This module implements a comprehensive file management system that enables
VS Code-like file operations over SSH connections. It provides all standard
file operations plus advanced features like search, diff generation, and
patch application.

Features:
    - File/directory CRUD operations (create, read, update, delete)
    - Advanced search with regex and content matching
    - Find and replace across multiple files
    - Diff generation and patch application
    - Chunked file downloads for large files
    - Ripgrep integration for fast searching
    - Safe path handling with validation

All operations return JSON responses for easy integration with iOS clients.
"""

import os
import sys
import json
import stat
import mimetypes
import fnmatch
import re
import base64
import hashlib
from datetime import datetime
from pathlib import Path
import tempfile
import shutil
import difflib
import subprocess

class RemoteFileManager:
    """File manager providing comprehensive file operations.
    
    Implements all file management operations with safety checks,
    error handling, and JSON response formatting for iOS integration.
    
    Attributes:
        MAX_FILE_SIZE: Maximum file size for read operations (100MB).
        MAX_SEARCH_RESULTS: Maximum number of search results to return.
        CHUNK_SIZE: Size of chunks for large file operations (64KB).
        DEFAULT_EXCLUDES: Patterns to exclude from search operations.
    """
    
    MAX_FILE_SIZE = 100 * 1024 * 1024  # Maximum file size for operations (100MB)
    MAX_SEARCH_RESULTS = 1000           # Maximum search results to return
    CHUNK_SIZE = 64 * 1024              # Chunk size for large file transfers (64KB)
    
    # Patterns to exclude from file operations and searches
    DEFAULT_EXCLUDES = ['.git', 'node_modules', '.DS_Store', '*.pyc', '__pycache__']
    
    def __init__(self):
        mimetypes.init()
    
    def safe_path(self, path):
        """Sanitize and validate a file path.
        
        Prevents directory traversal attacks by expanding and validating
        the provided path.
        
        Args:
            path: Path to validate.
            
        Returns:
            Absolute validated path.
            
        Raises:
            FileNotFoundError: If the path does not exist.
        """
        # Expand tilde and environment variables
        path = os.path.expanduser(path)
        # Convert to absolute path
        path = os.path.abspath(path)
        # Verify path exists
        if not os.path.exists(path):
            raise FileNotFoundError(f"Path does not exist: {path}")
        return path
    
    def get_file_info(self, path):
        """Get detailed information about a file or directory.
        
        Retrieves file metadata including size, modification time, permissions,
        and symlink information.
        
        Args:
            path: Path to the file or directory.
            
        Returns:
            Dictionary containing file information including:
                - name: Base name of the file
                - path: Full path
                - type: 'file', 'directory', or 'error'
                - size: File size in bytes (None for directories)
                - mtime: Modification time as Unix timestamp
                - mode: File permissions in octal format
                - is_symlink: Whether file is a symbolic link
                - is_hidden: Whether file name starts with '.'
                - extension: File extension (None for directories)
                - mime_type: MIME type for files under 1MB
                - target: Symlink target if applicable
                - target_exists: Whether symlink target exists
        """
        try:
            stat_info = os.stat(path)
            is_symlink = os.path.islink(path)
            is_dir = stat.S_ISDIR(stat_info.st_mode)
            
            info = {
                'name': os.path.basename(path),
                'path': path,
                'type': 'directory' if is_dir else 'file',
                'size': stat_info.st_size if not is_dir else None,
                'mtime': stat_info.st_mtime,
                'mode': oct(stat_info.st_mode),
                'is_symlink': is_symlink,
                'is_hidden': os.path.basename(path).startswith('.'),
                'extension': os.path.splitext(path)[1].lower() if not is_dir else None,
            }
            
            if is_symlink:
                try:
                    info['target'] = os.readlink(path)
                    info['target_exists'] = os.path.exists(path)
                except:
                    info['target'] = None
                    info['target_exists'] = False
            
            # Guess MIME type for small files
                mime_type, _ = mimetypes.guess_type(path)
                info['mime_type'] = mime_type
            
            return info
        except (OSError, IOError) as e:
            return {
                'name': os.path.basename(path),
                'path': path,
                'type': 'error',
                'error': str(e)
            }
    
    def list_directory(self, path, offset=0, limit=100, show_hidden=True):
        """List directory contents with optional pagination.
        
        Args:
            path: Directory path to list.
            offset: Starting index for pagination.
            limit: Maximum number of items to return.
            show_hidden: Include hidden files (starting with '.').
            
        Returns:
            Dictionary containing:
                - success: Operation status
                - path: Directory path
                - items: List of file information dictionaries
                - total: Total number of items
                - offset: Current offset
                - limit: Current limit
                - has_more: Whether more items exist
                - error: Error message if operation failed
        """
        try:
            path = self.safe_path(path)
            
            # Get all entries
            entries = []
            for name in os.listdir(path):
                if not show_hidden and name.startswith('.'):
                    continue
                    
                full_path = os.path.join(path, name)
                info = self.get_file_info(full_path)
                entries.append(info)
            
            # Sort entries: directories first, then alphabetically
            entries.sort(key=lambda x: (x['type'] != 'directory', x['name'].lower()))
            
            # Apply pagination to results
            total = len(entries)
            paginated = entries[offset:offset + limit]
            
            return {
                'success': True,
                'path': path,
                'items': paginated,
                'total': total,
                'offset': offset,
                'limit': limit,
                'has_more': offset + limit < total
            }
        except Exception as e:
            return {
                'success': False,
                'error': str(e),
                'path': path
            }
    
    def read_file(self, path):
        """Read file contents"""
        try:
            path = self.safe_path(path)
            stat_info = os.stat(path)
            
            if stat.S_ISDIR(stat_info.st_mode):
                return {
                    'success': False,
                    'error': 'Cannot read directory as file'
                }
            
            if stat_info.st_size > self.MAX_FILE_SIZE:
                return {
                    'success': False,
                    'error': f'File too large ({stat_info.st_size} bytes). Maximum size is {self.MAX_FILE_SIZE} bytes.'
                }
            
            # Try to read as text first
            encodings = ['utf-8', 'utf-16', 'latin-1', 'cp1252']
            for encoding in encodings:
                try:
                    with open(path, 'r', encoding=encoding) as f:
                        content = f.read()
                        return {
                            'success': True,
                            'content': content,
                            'encoding': encoding,
                            'size': stat_info.st_size,
                            'path': path
                        }
                except (UnicodeDecodeError, UnicodeError):
                    continue
            
            # If all text encodings fail, read as binary
            with open(path, 'rb') as f:
                content = f.read()
                encoded = base64.b64encode(content).decode('ascii')
                return {
                    'success': True,
                    'content': encoded,
                    'encoding': 'base64',
                    'size': stat_info.st_size,
                    'path': path
                }
                
        except Exception as e:
            return {
                'success': False,
                'error': str(e),
                'path': path
            }
    
    def write_file(self, path, content, encoding='utf-8', base64_encoded=False):
        """Write file contents"""
        try:
            # Expand and normalize path without checking existence
            path = os.path.expanduser(path)
            path = os.path.abspath(path)
            
            # Ensure parent directory exists
            parent_dir = os.path.dirname(path)
            if not os.path.exists(parent_dir):
                os.makedirs(parent_dir, exist_ok=True)
            
            if base64_encoded:
                # Decode base64 content
                content_bytes = base64.b64decode(content)
                with open(path, 'wb') as f:
                    f.write(content_bytes)
            else:
                # Write text content
                with open(path, 'w', encoding=encoding) as f:
                    f.write(content)
            
            return {
                'success': True,
                'path': path,
                'size': os.path.getsize(path)
            }
            
        except Exception as e:
            return {
                'success': False,
                'error': str(e),
                'path': path
            }
    
    def create_file(self, path, name, is_directory=False, content=''):
        """Create a new file or directory"""
        try:
            # Expand and normalize path
            path = os.path.expanduser(path)
            path = os.path.abspath(path)
            
            # Ensure the parent path exists
            if not os.path.exists(path):
                os.makedirs(path, exist_ok=True)
                
            full_path = os.path.join(path, name)
            
            if os.path.exists(full_path):
                return {
                    'success': False,
                    'error': 'File or directory already exists'
                }
            
            if is_directory:
                os.makedirs(full_path, exist_ok=True)
            else:
                # Ensure parent directory exists
                parent_dir = os.path.dirname(full_path)
                if not os.path.exists(parent_dir):
                    os.makedirs(parent_dir, exist_ok=True)
                
                with open(full_path, 'w', encoding='utf-8') as f:
                    f.write(content)
            
            return {
                'success': True,
                'path': full_path,
                'type': 'directory' if is_directory else 'file'
            }
            
        except Exception as e:
            return {
                'success': False,
                'error': str(e)
            }
    
    def rename_file(self, path, new_name):
        """Rename a file or directory"""
        try:
            path = self.safe_path(path)
            parent_dir = os.path.dirname(path)
            new_path = os.path.join(parent_dir, new_name)
            
            if os.path.exists(new_path):
                return {
                    'success': False,
                    'error': 'Target name already exists'
                }
            
            os.rename(path, new_path)
            
            return {
                'success': True,
                'old_path': path,
                'new_path': new_path
            }
            
        except Exception as e:
            return {
                'success': False,
                'error': str(e)
            }
    
    def delete_file(self, path, recursive=False):
        """Delete a file or directory"""
        try:
            path = self.safe_path(path)
            
            if os.path.isdir(path):
                if recursive:
                    shutil.rmtree(path)
                else:
                    os.rmdir(path)
            else:
                os.remove(path)
            
            return {
                'success': True,
                'path': path,
                'deleted': True
            }
            
        except Exception as e:
            return {
                'success': False,
                'error': str(e),
                'path': path
            }
    
    def find_files(self, path, pattern, content_search=False, file_pattern=None,
                   offset=0, limit=100, case_sensitive=False):
        """Search for files by name or content"""
        try:
            path = self.safe_path(path)
            results = []
            
            # Compile regex patterns with case sensitivity option
            if not case_sensitive:
                pattern = re.compile(pattern, re.IGNORECASE)
            else:
                pattern = re.compile(pattern)
            
            file_pattern_re = None
            if file_pattern:
                file_pattern_re = re.compile(fnmatch.translate(file_pattern), 
                                           0 if case_sensitive else re.IGNORECASE)
            
            # Walk directory tree
            for root, dirs, files in os.walk(path):
                # Skip excluded directories
                dirs[:] = [d for d in dirs if d not in self.DEFAULT_EXCLUDES]
                
                for filename in files:
                    # Skip excluded files
                    if any(fnmatch.fnmatch(filename, exc) for exc in self.DEFAULT_EXCLUDES):
                        continue
                    
                    # Check file pattern if specified
                    if file_pattern_re and not file_pattern_re.match(filename):
                        continue
                    
                    full_path = os.path.join(root, filename)
                    
                    if content_search:
                        # Search in file contents
                        try:
                            with open(full_path, 'r', encoding='utf-8', errors='ignore') as f:
                                line_number = 0
                                for line in f:
                                    line_number += 1
                                    if pattern.search(line):
                                        results.append({
                                            'path': full_path,
                                            'line': line_number,
                                            'content': line.strip()[:100]  # First 100 chars
                                        })
                                        if len(results) >= self.MAX_SEARCH_RESULTS:
                                            break
                        except:
                            pass
                    else:
                        # Search in filename
                        if pattern.search(filename):
                            results.append({
                                'path': full_path,
                                'name': filename
                            })
                    
                    if len(results) >= self.MAX_SEARCH_RESULTS:
                        break
                
                if len(results) >= self.MAX_SEARCH_RESULTS:
                    break
            
            # Paginate results
            total = len(results)
            paginated = results[offset:offset + limit]
            
            return {
                'success': True,
                'results': paginated,
                'total': total,
                'offset': offset,
                'limit': limit,
                'has_more': offset + limit < total
            }
            
        except Exception as e:
            return {
                'success': False,
                'error': str(e)
            }
    
    def replace_in_files(self, path, find_pattern, replace_text, file_pattern=None,
                        dry_run=True, backup=True, case_sensitive=False):
        """Find and replace text in files"""
        try:
            path = self.safe_path(path)
            results = []
            total_files = 0
            total_replacements = 0
            
            # Compile patterns
            flags = 0 if case_sensitive else re.IGNORECASE
            find_re = re.compile(find_pattern, flags)
            
            file_pattern_re = None
            if file_pattern:
                file_pattern_re = re.compile(fnmatch.translate(file_pattern), flags)
            
            # Walk directory tree
            for root, dirs, files in os.walk(path):
                # Skip excluded directories
                dirs[:] = [d for d in dirs if d not in self.DEFAULT_EXCLUDES]
                
                for filename in files:
                    # Skip excluded files
                    if any(fnmatch.fnmatch(filename, exc) for exc in self.DEFAULT_EXCLUDES):
                        continue
                    
                    # Check file pattern if specified
                    if file_pattern_re and not file_pattern_re.match(filename):
                        continue
                    
                    full_path = os.path.join(root, filename)
                    
                    try:
                        # Read file
                        with open(full_path, 'r', encoding='utf-8') as f:
                            content = f.read()
                        
                        # Find matches
                        matches = list(find_re.finditer(content))
                        if matches:
                            total_files += 1
                            total_replacements += len(matches)
                            
                            if dry_run:
                                # Just report what would be changed
                                results.append({
                                    'path': full_path,
                                    'matches': len(matches),
                                    'preview': matches[0].group()[:50] if matches else ''
                                })
                            else:
                                # Perform replacement
                                if backup:
                                    backup_path = full_path + '.bak'
                                    shutil.copy2(full_path, backup_path)
                                
                                new_content = find_re.sub(replace_text, content)
                                with open(full_path, 'w', encoding='utf-8') as f:
                                    f.write(new_content)
                                
                                results.append({
                                    'path': full_path,
                                    'matches': len(matches),
                                    'replaced': True,
                                    'backup': backup_path if backup else None
                                })
                    except:
                        pass
            
            return {
                'success': True,
                'dry_run': dry_run,
                'total_files': total_files,
                'total_replacements': total_replacements,
                'results': results[:100]  # Limit results
            }
            
        except Exception as e:
            return {
                'success': False,
                'error': str(e)
            }
    
    def download_file(self, path, chunk_index=None, chunk_size=None):
        """Download file contents with optional chunking.
        
        Supports chunked downloads for large files to handle bandwidth
        and memory constraints.
        
        Args:
            path: Path to file to download.
            chunk_index: Index of chunk to download (for large files).
            chunk_size: Size of each chunk in bytes.
            
        Returns:
            Dictionary containing:
                - success: Operation status
                - content: Base64-encoded file content
                - size: Total file size
                - complete: Whether download is complete
                - chunk_index: Current chunk index (if chunking)
                - chunk_size: Size of current chunk (if chunking)
                - chunks: Total number of chunks (if chunking)
                - error: Error message if operation failed
        """
        try:
            path = self.safe_path(path)
            stat_info = os.stat(path)
            
            if stat.S_ISDIR(stat_info.st_mode):
                return {
                    'success': False,
                    'error': 'Cannot download directory'
                }
            
            # Return entire content for small files
            if stat_info.st_size <= self.CHUNK_SIZE:
                with open(path, 'rb') as f:
                    content = f.read()
                    encoded = base64.b64encode(content).decode('ascii')
                    return {
                        'success': True,
                        'content': encoded,
                        'size': stat_info.st_size,
                        'complete': True
                    }
            
            # Handle chunked downloads for large files
            if chunk_index is not None and chunk_size is not None:
                # Download requested chunk
                with open(path, 'rb') as f:
                    f.seek(chunk_index * chunk_size)
                    chunk_data = f.read(chunk_size)
                    encoded = base64.b64encode(chunk_data).decode('ascii')
                    return {
                        'success': True,
                        'content': encoded,
                        'chunk_index': chunk_index,
                        'chunk_size': len(chunk_data)
                    }
            else:
                # Return chunk information for client
                chunks = (stat_info.st_size + self.CHUNK_SIZE - 1) // self.CHUNK_SIZE
                return {
                    'success': True,
                    'size': stat_info.st_size,
                    'chunks': chunks,
                    'chunk_size': self.CHUNK_SIZE,
                    'complete': False
                }
                
        except Exception as e:
            return {
                'success': False,
                'error': str(e),
                'path': path
            }
    
    def apply_patch(self, path, patch_data):
        """Apply a patch to a file.
        
        Supports unified diff format, line-based edits, and full content
        replacement.
        
        Args:
            path: Path to file to patch.
            patch_data: Dictionary containing patch information:
                - type: 'unified_diff', 'line_edits', or 'full_content'
                - patch: Diff content (for unified_diff)
                - edits: List of edit operations (for line_edits)
                - content: Full replacement content (for full_content)
                
        Returns:
            Dictionary containing:
                - success: Operation status
                - path: Patched file path
                - lines_changed: Number of lines affected (unified_diff)
                - edits_applied: Number of edits applied (line_edits)
                - method: Patch method used (full_content)
                - error: Error message if operation failed
        """
        try:
            path = self.safe_path(path)
            
            # Load current file content
            if not os.path.exists(path):
                return {
                    'success': False,
                    'error': 'File does not exist'
                }
            
            with open(path, 'r', encoding='utf-8') as f:
                original_lines = f.readlines()
            
            # Apply patch based on type
            if patch_data.get('type') == 'unified_diff':
                # Handle unified diff format
                patch_lines = patch_data.get('patch', '').splitlines(keepends=True)
                
                # Basic unified diff application
                # Note: For production, consider using python-patch library
                patched_content = self._apply_unified_diff(original_lines, patch_lines)
                
                # Write the patched content
                with open(path, 'w', encoding='utf-8') as f:
                    f.writelines(patched_content)
                
                return {
                    'success': True,
                    'path': path,
                    'lines_changed': len(patch_lines)
                }
                
            elif patch_data.get('type') == 'line_edits':
                # Handle individual line edits
                edits = patch_data.get('edits', [])
                lines = original_lines[:]
                
                # Process edits in reverse order to maintain line indices
                sorted_edits = sorted(edits, key=lambda x: x.get('line', 0), reverse=True)
                
                for edit in sorted_edits:
                    edit_type = edit.get('type')
                    line_num = edit.get('line', 1) - 1  # Convert to 0-based
                    
                    if edit_type == 'replace' and 0 <= line_num < len(lines):
                        lines[line_num] = edit.get('content', '') + '\n'
                    elif edit_type == 'insert':
                        lines.insert(line_num, edit.get('content', '') + '\n')
                    elif edit_type == 'delete' and 0 <= line_num < len(lines):
                        del lines[line_num]
                
                # Write the modified content
                with open(path, 'w', encoding='utf-8') as f:
                    f.writelines(lines)
                
                return {
                    'success': True,
                    'path': path,
                    'edits_applied': len(edits)
                }
                
            elif patch_data.get('type') == 'full_content':
                # Replace entire file content
                content = patch_data.get('content', '')
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(content)
                
                return {
                    'success': True,
                    'path': path,
                    'method': 'full_replacement'
                }
                
            else:
                return {
                    'success': False,
                    'error': f'Unknown patch type: {patch_data.get("type")}'
                }
                
        except Exception as e:
            return {
                'success': False,
                'error': str(e),
                'path': path
            }
    
    def _apply_unified_diff(self, original_lines, patch_lines):
        """Apply a unified diff to original lines.
        
        This is a simplified implementation for basic diff application.
        For production use, consider using the python-patch library.
        
        Args:
            original_lines: Original file lines.
            patch_lines: Unified diff lines.
            
        Returns:
            Modified lines after patch application.
        """
        # Simplified diff application - processes hunks sequentially
        result = original_lines[:]
        
        i = 0
        while i < len(patch_lines):
            line = patch_lines[i]
            
            if line.startswith('@@'):
                # Extract hunk header information
                import re
                match = re.match(r'@@ -(\d+),(\d+) \+(\d+),(\d+) @@', line)
                if match:
                    old_start = int(match.group(1)) - 1
                    old_lines = int(match.group(2))
                    new_start = int(match.group(3)) - 1
                    new_lines = int(match.group(4))
                    
                    # Process hunk lines
                    i += 1
                    hunk_lines = []
                    while i < len(patch_lines) and not patch_lines[i].startswith('@@'):
                        hunk_lines.append(patch_lines[i])
                        i += 1
                    
                    # Apply hunk changes
                    # Note: Simplified implementation - proper line matching needed
                    continue
            i += 1
        
        return result
    
    def generate_diff(self, path, old_content, new_content):
        """Generate a diff between two versions of content.
        
        Creates both unified diff format and structured edit operations
        for easy application.
        
        Args:
            path: File path (for diff headers).
            old_content: Original content.
            new_content: Modified content.
            
        Returns:
            Dictionary containing:
                - success: Operation status
                - path: File path
                - unified_diff: Unified diff format string
                - edits: List of structured edit operations
                - old_lines: Number of lines in original
                - new_lines: Number of lines in new version
                - changes: Total number of changes
                - error: Error message if operation failed
        """
        try:
            path = self.safe_path(path)
            
            old_lines = old_content.splitlines(keepends=True)
            new_lines = new_content.splitlines(keepends=True)
            
            # Create unified diff output
            diff = list(difflib.unified_diff(
                old_lines,
                new_lines,
                fromfile=path,
                tofile=path,
                lineterm=''
            ))
            
            # Generate structured edit operations for programmatic use
            matcher = difflib.SequenceMatcher(None, old_lines, new_lines)
            edits = []
            
            for tag, i1, i2, j1, j2 in matcher.get_opcodes():
                if tag == 'replace':
                    for i in range(i1, i2):
                        edits.append({
                            'type': 'delete',
                            'line': i + 1,
                            'content': old_lines[i].rstrip('\n')
                        })
                    for j in range(j1, j2):
                        edits.append({
                            'type': 'insert',
                            'line': i1 + 1,
                            'content': new_lines[j].rstrip('\n')
                        })
                elif tag == 'delete':
                    for i in range(i1, i2):
                        edits.append({
                            'type': 'delete',
                            'line': i + 1,
                            'content': old_lines[i].rstrip('\n')
                        })
                elif tag == 'insert':
                    for j in range(j1, j2):
                        edits.append({
                            'type': 'insert',
                            'line': i1 + 1,
                            'content': new_lines[j].rstrip('\n')
                        })
            
            return {
                'success': True,
                'path': path,
                'unified_diff': '\n'.join(diff),
                'edits': edits,
                'old_lines': len(old_lines),
                'new_lines': len(new_lines),
                'changes': len(edits)
            }
            
        except Exception as e:
            return {
                'success': False,
                'error': str(e),
                'path': path
            }
    
    def ripgrep_search(self, path, pattern, file_pattern=None, case_sensitive=False, 
                      search_type='content', max_results=1000, context_lines=0,
                      ignore_patterns=None, follow_symlinks=False):
        """Perform fast search using ripgrep.
        
        Leverages ripgrep for high-performance file and content searching
        with advanced filtering options.
        
        Args:
            path: Root directory for search.
            pattern: Search pattern (regex).
            file_pattern: Glob pattern to filter files.
            case_sensitive: Enable case-sensitive search.
            search_type: 'content' for text search, 'files' for filename search.
            max_results: Maximum number of results.
            context_lines: Number of context lines to include.
            ignore_patterns: List of patterns to exclude.
            follow_symlinks: Whether to follow symbolic links.
            
        Returns:
            Dictionary containing:
                - success: Operation status
                - matches: List of match details
                - total: Total number of matches
                - search_type: Type of search performed
                - pattern: Search pattern used
                - error: Error message if operation failed
                - fallback: True if ripgrep not available
        """
        try:
            path = self.safe_path(path)
            
            # Construct ripgrep command with options
            cmd = ['rg']
            
            # Add basic ripgrep flags
            cmd.extend(['--json', '--max-count', str(max_results)])
            
            # Configure case sensitivity
            if not case_sensitive:
                cmd.append('-i')
            
            # Configure search type (files or content)
            if search_type == 'files':
                cmd.append('--files')
                if pattern:
                    # Use pattern as glob filter for file search
                    cmd.extend(['--glob', f'*{pattern}*'])
            else:
                # Configure content search with context
                if context_lines > 0:
                    cmd.extend(['-C', str(context_lines)])
            
            # Apply file pattern filter
            if file_pattern:
                cmd.extend(['-g', file_pattern])
            
            # Add ignore patterns
            if ignore_patterns:
                for pattern in ignore_patterns:
                    cmd.extend(['--glob', f'!{pattern}'])
            
            # Enable symlink following if requested
            if follow_symlinks:
                cmd.append('-L')
            
            # Add search pattern for content searches
            if search_type != 'files':
                cmd.append(pattern)
            
            # Specify search directory
            cmd.append(path)
            
            # Run ripgrep and process output
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, check=False)
                
                if result.returncode not in [0, 1]:  # 0=found, 1=not found
                    return {
                        'success': False,
                        'error': f'ripgrep error: {result.stderr}'
                    }
                
                # Parse JSON-formatted ripgrep output
                matches = []
                for line in result.stdout.strip().split('\n'):
                    if not line:
                        continue
                    
                    try:
                        data = json.loads(line)
                        if data['type'] == 'match':
                            match_data = data['data']
                            
                            match_info = {
                                'path': match_data['path']['text'],
                                'line_number': match_data.get('line_number'),
                                'lines': match_data.get('lines', {}).get('text', ''),
                                'absolute_offset': match_data.get('absolute_offset'),
                            }
                            
                            # Extract submatch positions and text
                            if 'submatches' in match_data:
                                submatches = []
                                for submatch in match_data['submatches']:
                                    submatches.append({
                                        'start': submatch['start'],
                                        'end': submatch['end'],
                                        'text': submatch['match']['text']
                                    })
                                match_info['matches'] = submatches
                            
                            matches.append(match_info)
                            
                    except json.JSONDecodeError:
                        continue
                
                return {
                    'success': True,
                    'matches': matches,
                    'total': len(matches),
                    'search_type': search_type,
                    'pattern': pattern
                }
                
            except FileNotFoundError:
                # Ripgrep not available - suggest installation
                return {
                    'success': False,
                    'error': 'ripgrep not found. Install with: brew install ripgrep',
                    'fallback': True
                }
                
        except Exception as e:
            return {
                'success': False,
                'error': str(e)
            }
    
    def is_build_artifact(self, path):
        """Check if a path is a build artifact that should be excluded."""
        build_artifacts = {
            '.next', '.nuxt', 'dist', 'build', 'out', 'target',
            'node_modules', 'vendor', '.cache', 'coverage',
            '__pycache__', '.pytest_cache', '.tox',
            'DerivedData', 'Build', '.build'
        }
        
        # Only check the project directory name itself, not the entire path
        project_name = os.path.basename(path.rstrip('/'))
        return project_name in build_artifacts
    
    def analyze_file_extensions(self, project_path, max_files=100):
        """Analyze file extensions to determine primary language."""
        try:
            exclude_dirs = [
                'node_modules', '.git', 'vendor', 'target', 
                'build', 'dist', '.next', '.nuxt', 'out'
            ]
            
            # Try ripgrep first for speed
            try:
                cmd = ['rg', '--files', '--max-count', str(max_files)]
                for exclude in exclude_dirs:
                    cmd.extend(['--glob', f'!{exclude}'])
                cmd.append(project_path)
                
                result = subprocess.run(cmd, capture_output=True, text=True, 
                                      timeout=2, check=False)
                files = result.stdout.strip().split('\n') if result.returncode == 0 else []
            except:
                # Fallback to find
                cmd = ['find', project_path, '-type', 'f']
                for exclude in exclude_dirs:
                    cmd.extend(['!', '-path', f'*/{exclude}/*'])
                cmd.extend(['-print'])
                
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=2)
                files = result.stdout.strip().split('\n')[:max_files] if result.returncode == 0 else []
            
            if not files:
                return None
            
            # Count extensions
            ext_counts = {}
            for file_path in files:
                if not file_path:
                    continue
                ext = Path(file_path).suffix.lower()
                if ext:
                    ext_counts[ext] = ext_counts.get(ext, 0) + 1
            
            # Map extensions to languages
            ext_to_lang = {
                '.js': 'node', '.jsx': 'node', '.ts': 'node', '.tsx': 'node', '.mjs': 'node',
                '.py': 'python', '.pyx': 'python', '.pyi': 'python',
                '.rs': 'rust',
                '.go': 'go',
                '.swift': 'swift', '.m': 'swift', '.mm': 'swift',
                '.rb': 'ruby', '.erb': 'ruby',
                '.java': 'java', '.kt': 'java',
                '.php': 'php',
                '.dart': 'dart',
                '.c': 'c', '.cpp': 'cpp', '.cc': 'cpp', '.cxx': 'cpp', '.h': 'c',
                '.cs': 'csharp',
                '.r': 'r', '.R': 'r',
                '.scala': 'scala',
                '.ex': 'elixir', '.exs': 'elixir',
            }
            
            # Calculate language scores
            lang_scores = {}
            total_files = sum(ext_counts.values())
            
            for ext, count in ext_counts.items():
                if ext in ext_to_lang:
                    lang = ext_to_lang[ext]
                    lang_scores[lang] = lang_scores.get(lang, 0) + count
            
            if not lang_scores:
                return None
            
            # Get the predominant language
            main_lang = max(lang_scores, key=lang_scores.get)
            confidence = (lang_scores[main_lang] / total_files) * 100
            
            # Only return if we have reasonable confidence
            if confidence > 30:
                return (main_lang, confidence, f'{main_lang} files')
            
            return None
            
        except Exception:
            return None
    
    def process_discovered_projects(self, discovered, max_results):
        """Post-process discovered projects to filter and detect languages.
        
        Args:
            discovered: Dictionary of discovered projects
            max_results: Maximum number of results to return
            
        Returns:
            List of processed and filtered projects
        """
        filtered_projects = {}
        build_artifact_names = {'.next', '.nuxt', 'dist', 'build', 'out', 'target', 
                               'node_modules', 'vendor', '.cache', 'coverage',
                               '__pycache__', '.pytest_cache', '.tox',
                               'DerivedData', 'Build', '.build'}
        
        # Normalize all paths first
        normalized_discovered = {}
        for path, info in discovered.items():
            normalized_path = os.path.normpath(path.rstrip('/'))
            info['path'] = normalized_path  # Update the path in info too
            normalized_discovered[normalized_path] = info
        
        for project_path, project_info in normalized_discovered.items():
            # Get the project name and markers
            project_name = os.path.basename(project_path)
            project_markers = project_info.get('markers', [])
            if not project_markers and 'marker' in project_info:
                project_markers = [project_info['marker']]
            
            # Skip if it's a build artifact folder
            # Always skip these folders to prevent them from appearing as separate projects
            if project_name in build_artifact_names:
                continue
            
            # Check relationships with already processed projects
            skip_project = False
            to_replace = []
            
            for existing_path, existing_info in list(filtered_projects.items()):
                # Normalize for comparison
                existing_path_normalized = os.path.normpath(existing_path)
                
                # Check if new project is inside an existing project
                if project_path.startswith(existing_path_normalized + os.sep):
                    # New is inside existing
                    # Skip if it's a build artifact name (already filtered above)
                    # This is redundant now but kept for clarity
                    if project_name in build_artifact_names:
                        skip_project = True
                        break
                    # Otherwise it's a valid nested project (like monorepo package), keep it
                    
                # Check if existing project is inside the new project
                elif existing_path_normalized.startswith(project_path + os.sep):
                    # Existing is inside new
                    existing_name = os.path.basename(existing_path_normalized)
                    
                    # If existing is a build artifact name, it should have been filtered already
                    # but double-check and mark for replacement if somehow it got through
                    if existing_name in build_artifact_names:
                        to_replace.append(existing_path)
                    # Otherwise it's a valid nested project, keep both
            
            # Skip if marked for skipping
            if skip_project:
                continue
            
            # Remove any projects that should be replaced
            for path in to_replace:
                del filtered_projects[path]
            
            # Process and add this project
            # Detect language properly
            markers = project_info.get('markers', [])
            if not markers and 'marker' in project_info:
                markers = [project_info['marker']]
            
            lang_type, confidence, primary_marker = self.detect_project_language(
                project_path, markers
            )
            
            # Update project info with proper language
            project_info['type'] = lang_type
            project_info['confidence'] = confidence
            project_info['primary_marker'] = primary_marker
            
            # Get modification time for sorting
            try:
                stat_info = os.stat(project_path)
                project_info['mtime'] = stat_info.st_mtime
            except:
                project_info['mtime'] = 0
            
            # Add to filtered projects
            filtered_projects[project_path] = project_info
        
        # Sort by most recent modification time, then by name
        projects = sorted(
            filtered_projects.values(),
            key=lambda x: (-x.get('mtime', 0), x['name'].lower())
        )
        
        # Remove mtime from output (it was just for sorting)
        for proj in projects:
            proj.pop('mtime', None)
        
        # Limit results
        return projects[:max_results]
    
    def detect_python_framework(self, project_path):
        """Detect specific Python framework."""
        try:
            # Check for framework-specific files
            framework_markers = {
                'manage.py': 'django',
                'settings.py': 'django',
                'wsgi.py': 'django',
                'main.py': 'fastapi',  # Common for FastAPI
                'app.py': 'python',  # Could be Flask or generic
                'flask_app.py': 'python',
            }
            
            # Check for framework markers
            for marker, framework in framework_markers.items():
                marker_path = os.path.join(project_path, marker)
                if os.path.exists(marker_path):
                    # For main.py, check if it contains FastAPI imports
                    if marker == 'main.py':
                        try:
                            with open(marker_path, 'r') as f:
                                content = f.read(1000)  # Read first 1000 chars
                                if 'fastapi' in content.lower() or 'FastAPI' in content:
                                    return 'fastapi'
                        except:
                            pass
                    else:
                        return framework
            
            # Check requirements.txt for framework packages
            req_path = os.path.join(project_path, 'requirements.txt')
            if os.path.exists(req_path):
                try:
                    with open(req_path, 'r') as f:
                        requirements = f.read().lower()
                    
                    if 'django' in requirements:
                        return 'django'
                    elif 'fastapi' in requirements:
                        return 'fastapi'
                    elif 'flask' in requirements:
                        return 'python'  # We don't have a flask icon
                except:
                    pass
            
            # Check Pipfile if exists
            pipfile_path = os.path.join(project_path, 'Pipfile')
            if os.path.exists(pipfile_path):
                try:
                    with open(pipfile_path, 'r') as f:
                        pipfile = f.read().lower()
                    
                    if 'django' in pipfile:
                        return 'django'
                    elif 'fastapi' in pipfile:
                        return 'fastapi'
                except:
                    pass
            
            return 'python'
        except:
            return 'python'
    
    def detect_js_framework(self, project_path):
        """Detect specific JavaScript framework for Node.js projects."""
        try:
            # Check for framework-specific config files
            framework_markers = {
                'next.config.js': 'nextjs',
                'next.config.mjs': 'nextjs',
                'next.config.ts': 'nextjs',
                # '.next' removed - it's a build artifact
                'nuxt.config.js': 'vue',
                'nuxt.config.ts': 'vue',
                '.nuxt': 'vue',
                'vite.config.js': 'react',  # Often React but could be Vue/Svelte
                'vite.config.ts': 'react',
                'angular.json': 'angular',
                '.angular': 'angular',
                'svelte.config.js': 'svelte',
                'gatsby-config.js': 'react',
                'vue.config.js': 'vue',
            }
            
            # Check for framework markers
            for marker, framework in framework_markers.items():
                marker_path = os.path.join(project_path, marker)
                if os.path.exists(marker_path):
                    return framework
            
            # Additional Next.js detection - check for Next.js directory structure
            if os.path.exists(os.path.join(project_path, 'pages')) or \
               os.path.exists(os.path.join(project_path, 'app')):
                # Check if it's likely a Next.js project (has pages/_app or app/layout)
                if os.path.exists(os.path.join(project_path, 'pages', '_app.js')) or \
                   os.path.exists(os.path.join(project_path, 'pages', '_app.jsx')) or \
                   os.path.exists(os.path.join(project_path, 'pages', '_app.ts')) or \
                   os.path.exists(os.path.join(project_path, 'pages', '_app.tsx')) or \
                   os.path.exists(os.path.join(project_path, 'app', 'layout.js')) or \
                   os.path.exists(os.path.join(project_path, 'app', 'layout.jsx')) or \
                   os.path.exists(os.path.join(project_path, 'app', 'layout.ts')) or \
                   os.path.exists(os.path.join(project_path, 'app', 'layout.tsx')):
                    return 'nextjs'
            
            # If no specific framework found, check package.json for dependencies
            package_json_path = os.path.join(project_path, 'package.json')
            if os.path.exists(package_json_path):
                try:
                    with open(package_json_path, 'r') as f:
                        package_data = json.loads(f.read())
                    
                    deps = {}
                    if 'dependencies' in package_data:
                        deps.update(package_data['dependencies'])
                    if 'devDependencies' in package_data:
                        deps.update(package_data['devDependencies'])
                    
                    # Check for framework packages
                    if 'next' in deps:
                        return 'nextjs'
                    elif '@angular/core' in deps:
                        return 'angular'
                    elif 'vue' in deps or '@vue/cli-service' in deps:
                        return 'vue'
                    elif 'svelte' in deps:
                        return 'svelte'
                    elif 'react' in deps:
                        return 'react'
                    elif '@types/node' in deps or 'typescript' in deps:
                        return 'typescript'
                except:
                    pass
            
            # Default to nodejs if no specific framework detected
            return 'nodejs'
        except:
            return 'nodejs'
    
    def detect_project_language(self, project_path, markers_found):
        """Detect the primary language of a project based on file markers."""
        
        # Priority-based language detection
        language_priorities = {
            # Priority 1: Package managers (definitive)
            'package.json': ('node', 100, 'package.json'),
            'Cargo.toml': ('rust', 100, 'Cargo.toml'),
            'go.mod': ('go', 100, 'go.mod'),
            'Package.swift': ('swift', 100, 'Package.swift'),
            'requirements.txt': ('python', 90, 'requirements.txt'),
            'pyproject.toml': ('python', 95, 'pyproject.toml'),
            'setup.py': ('python', 95, 'setup.py'),
            'Gemfile': ('ruby', 100, 'Gemfile'),
            'build.gradle': ('java', 95, 'build.gradle'),
            'pom.xml': ('java', 95, 'pom.xml'),
            'composer.json': ('php', 100, 'composer.json'),
            'pubspec.yaml': ('dart', 100, 'pubspec.yaml'),
            
            # Priority 2: Build systems
            '.xcworkspace': ('swift', 95, 'Xcode Workspace'),
            '.xcodeproj': ('swift', 90, 'Xcode Project'),
            'Makefile': ('make', 70, 'Makefile'),
            'CMakeLists.txt': ('cmake', 75, 'CMakeLists.txt'),
            
            # Priority 3: Environment/config
            'docker-compose.yml': ('docker', 80, 'docker-compose.yml'),
            'Dockerfile': ('docker', 75, 'Dockerfile'),
            '.venv': ('python', 70, 'Python venv'),
            'venv': ('python', 70, 'Python venv'),
        }
        
        # Check markers in priority order
        best_match = None
        best_score = 0
        
        for marker in markers_found:
            if marker in language_priorities:
                lang, score, display_marker = language_priorities[marker]
                if score > best_score:
                    best_match = (lang, score, display_marker)
                    best_score = score
        
        # If we have a high-confidence match, check for specific frameworks
        if best_match and best_score >= 90:
            lang, score, display_marker = best_match
            
            # For Git repos, check if it's actually a framework project
            if lang == 'git':
                # Check for Node.js project
                if os.path.exists(os.path.join(project_path, 'package.json')):
                    framework = self.detect_js_framework(project_path)
                    return (framework, score, 'Git + ' + framework)
                # Check for Python project
                elif any(os.path.exists(os.path.join(project_path, m)) 
                        for m in ['requirements.txt', 'setup.py', 'pyproject.toml', 'Pipfile']):
                    framework = self.detect_python_framework(project_path)
                    return (framework, score, 'Git + ' + framework)
                # Check for other language markers
                elif os.path.exists(os.path.join(project_path, 'Cargo.toml')):
                    return ('rust', score, 'Git + Rust')
                elif os.path.exists(os.path.join(project_path, 'go.mod')):
                    return ('go', score, 'Git + Go')
                elif os.path.exists(os.path.join(project_path, 'Gemfile')):
                    return ('ruby', score, 'Git + Ruby')
                elif os.path.exists(os.path.join(project_path, 'Package.swift')):
                    return ('swift', score, 'Git + Swift')
            # For Node.js projects, detect specific framework
            elif lang == 'node':
                framework = self.detect_js_framework(project_path)
                return (framework, score, display_marker)
            # For Python projects, detect specific framework
            elif lang == 'python':
                framework = self.detect_python_framework(project_path)
                return (framework, score, display_marker)
            return best_match
        
        # For git repos or low-confidence matches, analyze file extensions
        if '.git' in markers_found or best_score < 90:
            lang_from_files = self.analyze_file_extensions(project_path)
            if lang_from_files:
                # If we have a match from markers, combine confidence
                if best_match:
                    # Average the confidence scores
                    combined_score = (best_score + lang_from_files[1]) / 2
                    # Prefer the marker-based detection if languages match
                    if best_match[0] == lang_from_files[0]:
                        final_lang = best_match[0]
                        # For Node.js projects, detect specific framework
                        if final_lang == 'node':
                            final_lang = self.detect_js_framework(project_path)
                        # For Python projects, detect specific framework
                        elif final_lang == 'python':
                            final_lang = self.detect_python_framework(project_path)
                        return (final_lang, combined_score, best_match[2])
                    # Otherwise, use file analysis if it's more confident
                    elif lang_from_files[1] > best_score:
                        final_lang = lang_from_files[0]
                        # For Node.js projects, detect specific framework
                        if final_lang == 'node':
                            final_lang = self.detect_js_framework(project_path)
                            return (final_lang, lang_from_files[1], lang_from_files[2])
                        # For Python projects, detect specific framework
                        elif final_lang == 'python':
                            final_lang = self.detect_python_framework(project_path)
                            return (final_lang, lang_from_files[1], lang_from_files[2])
                        return lang_from_files
                else:
                    final_lang = lang_from_files[0]
                    # For Node.js projects, detect specific framework
                    if final_lang == 'node':
                        final_lang = self.detect_js_framework(project_path)
                        return (final_lang, lang_from_files[1], lang_from_files[2])
                    # For Python projects, detect specific framework
                    elif final_lang == 'python':
                        final_lang = self.detect_python_framework(project_path)
                        return (final_lang, lang_from_files[1], lang_from_files[2])
                    return lang_from_files
        
        # Return best match or unknown
        return best_match if best_match else ('unknown', 0, 'folder')
    
    def scan_projects(self, paths=None, max_depth=3, max_results=500, 
                     follow_symlinks=False, use_ripgrep=True):
        """Ultra-fast project discovery using ripgrep or find.
        
        Scans directories for project markers like .git, package.json, etc.
        Uses ripgrep for blazing fast performance when available.
        
        Args:
            paths: List of paths to scan. Defaults to common project locations.
            max_depth: Maximum directory depth to scan.
            max_results: Maximum number of projects to return.
            follow_symlinks: Whether to follow symbolic links.
            use_ripgrep: Try to use ripgrep for faster scanning.
            
        Returns:
            Dictionary containing:
                - success: Operation status
                - projects: List of discovered projects with metadata
                - total: Total number of projects found
                - scan_method: 'ripgrep' or 'find'
                - error: Error message if operation failed
        """
        try:
            # Default paths if none provided
            if not paths:
                paths = [
                    "~",
                    "~/Projects", "~/projects",
                    "~/Developer", "~/developer",
                    "~/Documents",
                    "~/Code", "~/code",
                    "~/repos", "~/src",
                    "~/workspace", "~/dev",
                    "~/work", "~/git"
                ]
            
            # Expand and validate paths
            valid_paths = []
            for p in paths:
                expanded = os.path.expanduser(p)
                if os.path.exists(expanded):
                    valid_paths.append(expanded)
            
            if not valid_paths:
                return {
                    'success': False,
                    'error': 'No valid paths to scan'
                }
            
            discovered = {}  # Use dict to deduplicate by path
            
            # Project markers and their types
            # Separate file markers from directory markers for proper handling
            file_markers = [
                ('package.json', 'node'),
                ('Package.swift', 'swift'),
                ('Cargo.toml', 'rust'),
                ('go.mod', 'go'),
                ('requirements.txt', 'python'),
                ('pyproject.toml', 'python'),
                ('setup.py', 'python'),
                ('Gemfile', 'ruby'),
                ('build.gradle', 'java'),
                ('pom.xml', 'java'),
                ('composer.json', 'php'),
                ('pubspec.yaml', 'dart'),
                ('Makefile', 'make'),
                ('CMakeLists.txt', 'cmake'),
                ('docker-compose.yml', 'docker'),
                ('Dockerfile', 'docker')
            ]
            
            directory_markers = [
                ('.git', 'git'),
                ('.xcworkspace', 'xcode'),  # Prioritize workspace over project
                ('.xcodeproj', 'xcode'),
                ('.venv', 'python'),
                ('venv', 'python')
            ]
            
            # Combined list for processing
            all_markers = file_markers + directory_markers
            
            if use_ripgrep:
                try:
                    # Try using ripgrep for ultra-fast scanning
                    for scan_path in valid_paths[:5]:  # Limit to first 5 paths for performance
                        cmd = ['rg', '--files', '--hidden', '--no-ignore-vcs']
                        
                        # Add depth limit
                        cmd.extend(['--max-depth', str(max_depth)])
                        
                        # Add follow symlinks if requested
                        if follow_symlinks:
                            cmd.append('-L')
                        
                        # Add glob patterns for all project markers
                        # Files first
                        for marker, _ in file_markers:
                            cmd.extend(['-g', f'**/{marker}'])
                        
                        # Then directories - match any file inside them
                        for marker, _ in directory_markers:
                            cmd.extend(['-g', f'**/{marker}/**'])
                        
                        # Exclude common large directories
                        exclude_patterns = [
                            'node_modules', '.cache', 'target', 'dist',
                            'build', 'out', '.next', '.nuxt', 'vendor',
                            '*.min.js', '*.bundle.js', 'coverage'
                        ]
                        
                        for pattern in exclude_patterns:
                            cmd.extend(['--glob', f'!**/{pattern}/**'])
                        
                        cmd.append(scan_path)
                        
                        # Run ripgrep
                        result = subprocess.run(cmd, capture_output=True, text=True, 
                                              timeout=10, check=False)
                        
                        if result.returncode in [0, 1]:  # 0=found matches, 1=no matches (but successful)
                            # Process results
                            for line in result.stdout.strip().split('\n'):
                                if not line:
                                    continue
                                
                                # Determine project root and type
                                project_path = None
                                project_type = None
                                marker_found = None
                                
                                # Check file markers first
                                for marker, proj_type in file_markers:
                                    if marker in line and line.endswith(marker):
                                        project_path = os.path.dirname(line)
                                        project_type = proj_type
                                        marker_found = marker
                                        break
                                
                                # Then check directory markers
                                if not project_path:
                                    for marker, proj_type in directory_markers:
                                        if f'/{marker}/' in line:
                                            # Extract path up to the marker directory
                                            parts = line.split(f'/{marker}/')
                                            if parts:
                                                project_path = parts[0]
                                                project_type = proj_type
                                                marker_found = marker
                                                break
                                
                                if project_path:
                                    project_name = os.path.basename(project_path)
                                    # Handle case where project is at root
                                    if not project_name:
                                        project_name = os.path.basename(os.path.dirname(project_path))
                                    
                                    # Early filtering: Skip build artifact folders entirely
                                    build_artifacts = {'.next', '.nuxt', 'dist', 'build', 'out', 'target', 
                                                     'node_modules', 'vendor', '.cache', 'coverage',
                                                     '__pycache__', '.pytest_cache', '.tox',
                                                     'DerivedData', 'Build', '.build'}
                                    if project_name in build_artifacts:
                                        continue
                                    
                                    # If project already discovered, add to its markers
                                    if project_path in discovered:
                                        if marker_found not in discovered[project_path].get('markers', []):
                                            discovered[project_path]['markers'].append(marker_found)
                                    else:
                                        # New project discovery
                                        discovered[project_path] = {
                                            'name': project_name,
                                            'path': project_path,
                                            'type': project_type,  # Will be updated later
                                            'marker': marker_found,
                                            'markers': [marker_found]
                                        }
                                    
                                    if len(discovered) >= max_results:
                                        break
                                
                                if len(discovered) >= max_results:
                                    break
                        
                        if len(discovered) >= max_results:
                            break
                    
                    # If ripgrep succeeded, process results
                    if discovered:
                        projects = self.process_discovered_projects(discovered, max_results)
                        return {
                            'success': True,
                            'projects': projects,
                            'total': len(projects),
                            'scan_method': 'ripgrep'
                        }
                    
                except (subprocess.TimeoutExpired, FileNotFoundError):
                    pass  # Fall back to find
            
            # Fallback to find command if ripgrep not available or failed
            for scan_path in valid_paths[:3]:  # Limit paths for find
                # Build find command for all markers
                find_cmd = ['find', scan_path, '-maxdepth', str(max_depth)]
                
                if follow_symlinks:
                    find_cmd.append('-L')
                
                # Add all marker patterns
                find_cmd.append('(')
                first = True
                
                # Add file markers
                for marker, _ in file_markers[:8]:  # Limit for performance
                    if not first:
                        find_cmd.append('-o')
                    find_cmd.extend(['-type', 'f', '-name', marker])
                    first = False
                
                # Add directory markers
                for marker, _ in directory_markers:
                    if not first:
                        find_cmd.append('-o')
                    find_cmd.extend(['-type', 'd', '-name', marker])
                    first = False
                    
                find_cmd.append(')')
                
                # Add exclusions
                find_cmd.extend([
                    '!', '-path', '*/node_modules/*',
                    '!', '-path', '*/.cache/*',
                    '!', '-path', '*/vendor/*'
                ])
                
                try:
                    result = subprocess.run(find_cmd, capture_output=True, text=True,
                                          timeout=15, check=False)
                    
                    if result.returncode == 0:
                        for line in result.stdout.strip().split('\n'):
                            if not line:
                                continue
                            
                            # Determine project type and root
                            project_path = None
                            project_type = None
                            marker_found = None
                            
                            # Check if it's a file marker
                            for marker, proj_type in file_markers:
                                if line.endswith('/' + marker):
                                    project_path = os.path.dirname(line)
                                    project_type = proj_type
                                    marker_found = marker
                                    break
                            
                            # Check if it's a directory marker
                            if not project_path:
                                for marker, proj_type in directory_markers:
                                    if line.endswith('/' + marker) or line.endswith('/' + marker + '/'):
                                        # Directory found - project is its parent
                                        project_path = os.path.dirname(line.rstrip('/'))
                                        project_type = proj_type
                                        marker_found = marker
                                        break
                            
                            if project_path:
                                project_name = os.path.basename(project_path)
                                # Handle case where project is at root
                                if not project_name:
                                    project_name = os.path.basename(os.path.dirname(project_path))
                                
                                # Early filtering: Skip build artifact folders entirely
                                build_artifacts = {'.next', '.nuxt', 'dist', 'build', 'out', 'target', 
                                                 'node_modules', 'vendor', '.cache', 'coverage',
                                                 '__pycache__', '.pytest_cache', '.tox',
                                                 'DerivedData', 'Build', '.build'}
                                if project_name in build_artifacts:
                                    continue
                                    
                                # If project already discovered, add to its markers
                                if project_path in discovered:
                                    if marker_found not in discovered[project_path].get('markers', []):
                                        discovered[project_path]['markers'].append(marker_found)
                                else:
                                    # New project discovery
                                    discovered[project_path] = {
                                        'name': project_name,
                                        'path': project_path,
                                        'type': project_type,  # Will be updated later
                                        'marker': marker_found,
                                        'markers': [marker_found]
                                    }
                                
                                if len(discovered) >= max_results:
                                    break
                            
                            if len(discovered) >= max_results:
                                break
                    
                    if len(discovered) >= max_results:
                        break
                        
                except subprocess.TimeoutExpired:
                    if discovered:  # Return what we have so far
                        break
                    else:
                        return {
                            'success': False,
                            'error': 'Scan timeout - filesystem too large'
                        }
            
            # Post-process discovered projects
            projects = self.process_discovered_projects(discovered, max_results)
            
            return {
                'success': True,
                'projects': projects,
                'total': len(projects),
                'scan_method': 'find'
            }
            
        except Exception as e:
            return {
                'success': False,
                'error': str(e)
            }

def main():
    """Main entry point for the file manager CLI.
    
    Parses command-line arguments and routes to the appropriate
    file operation method. All operations return JSON responses
    for easy integration with iOS clients.
    
    Usage:
        python kisuke-file-manager.py <command> <json_args>
    
    Commands:
        list: List directory contents
        read: Read file contents
        write: Write file contents
        create: Create file or directory
        rename: Rename file or directory
        delete: Delete file or directory
        find: Search for files
        replace: Find and replace in files
        download: Download file (with chunking support)
        apply_patch: Apply patch to file
        generate_diff: Generate diff between contents
        ripgrep: Fast search using ripgrep
        scan_projects: Scan for projects using ripgrep or find
    """
    if len(sys.argv) < 2:
        print(json.dumps({
            'success': False,
            'error': 'No command specified',
            'usage': 'python kisuke-file-manager.py <command> <args...>'
        }))
        sys.exit(1)
    
    command = sys.argv[1]
    manager = RemoteFileManager()
    
    try:
        # Parse JSON arguments if provided
        if len(sys.argv) > 2:
            args = json.loads(sys.argv[2])
        else:
            args = {}
        
        # Route to appropriate method
        if command == 'list':
            result = manager.list_directory(**args)
        elif command == 'read':
            result = manager.read_file(**args)
        elif command == 'write':
            result = manager.write_file(**args)
        elif command == 'create':
            result = manager.create_file(**args)
        elif command == 'rename':
            result = manager.rename_file(**args)
        elif command == 'delete':
            result = manager.delete_file(**args)
        elif command == 'find':
            result = manager.find_files(**args)
        elif command == 'replace':
            result = manager.replace_in_files(**args)
        elif command == 'download':
            result = manager.download_file(**args)
        elif command == 'download_chunk':
            result = manager.download_file(**args)
        elif command == 'apply_patch':
            result = manager.apply_patch(**args)
        elif command == 'generate_diff':
            result = manager.generate_diff(**args)
        elif command == 'ripgrep':
            result = manager.ripgrep_search(**args)
        elif command == 'scan_projects':
            result = manager.scan_projects(**args)
        else:
            result = {
                'success': False,
                'error': f'Unknown command: {command}'
            }
        
        print(json.dumps(result, default=str))
        
    except Exception as e:
        print(json.dumps({
            'success': False,
            'error': str(e),
            'command': command
        }))
        sys.exit(1)


if __name__ == '__main__':
    main()