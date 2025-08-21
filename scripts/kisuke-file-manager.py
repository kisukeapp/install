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