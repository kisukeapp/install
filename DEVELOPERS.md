# Developer Instructions

Internal documentation for Kisuke app integration.

## Repository Structure

```
install/                    # Main repository
├── setup.sh               # Main installer script
├── scripts/               # Runtime scripts deployed to ~/.kisuke/scripts
│   ├── kisuke-broker.py
│   ├── kisuke-login.py
│   ├── kisuke-forwarder.py
│   └── kisuke-file-manager.py
├── files/                 # Platform-specific installers
│   └── {distro}/
│       ├── dependencies/ # System dependency installers
│       └── packages/     # Binary package installers
└── utils/                 # Helper scripts
```

## Integration with iOS App

### Initial Setup
1. iOS app downloads this repository
2. Executes `install.sh` to detect platform
3. Runs `setup.sh --install --app` for KECHO-formatted output
4. Monitors progress via KECHO messages

### KECHO Protocol
The `--app` flag enables machine-readable output for the iOS app:
- `[KECHO] OK` - Success messages
- `[KECHO] NOTIFY` - Progress updates  
- `[KECHO] ALERT` - Errors or missing dependencies

Example outputs:
```
[KECHO] OK SYSTEM_INFO OS=mac ARCH=arm64
[KECHO] OK LOCAL_BIN nodejs up-to-date (v22.9.0)
[KECHO] ALERT LOCAL_BIN PACKAGE_NOT_FOUND python3
```

### Build Steps
Run scripts in order:
```
# detarmine the OS and ARCH we are working on
bash install.sh

# bundle packages to $HOME
bash package.sh

# upload artifact (nodejs, python3, setup.sh, bridge scripts) Requires: GITHUB_TOKEN
bash utils/upload-artifacts.sh
```

### Environment Setup
Add to SSH session to make binaries available:
```bash
export PATH="$HOME/.kisuke/bin:$PATH"
```

## Platform Detection

Running `install.sh` provides:
- Platform: `[KECHO] DEV_ENVIRONMENT macos`
- Dependencies: `[KECHO] DEPENDENCIES git tmux ...`
- Packages: `[KECHO] PACKAGES nodejs python3 ...`
- Installed: `[KECHO] ALERT INSTALLED git tmux`
- Missing: `[KECHO] ALERT MISSING_DEPENDENCIES nodejs`

## Package Management

### Installing Dependencies
```bash
bash files/{$DISTRO}/dependencies/{PACKAGE_NAME}.sh
```

### Installing Packages  
```bash
bash files/{$DISTRO}/packages/{PACKAGE_NAME}.sh
```

### Binary Locations
All binaries are installed to `$HOME/.kisuke/bin/` to avoid system pollution:
- `nodejs/` - Node.js installation
- `python3/` - Python installation
- `jq` - Direct binary
- Symlinks for easy access (node, npm, python, pip, etc.)

## Adding New Packages

1. Update version in `setup.sh` VERSIONS array
2. Create installer script in appropriate `files/{distro}/packages/` directory
3. Test on all supported platforms
4. Update documentation

## Script Deployment

The `--deploy` flag copies runtime scripts from `scripts/` to `~/.kisuke/scripts/`:
- Used by iOS app for broker connections
- Automatically deployed during `--install`
- Can be updated separately with `--deploy`

## Supported Platforms

### macOS
- Requires Homebrew for some dependencies
- Supports both Intel (x86_64) and Apple Silicon (arm64)

### Linux Distributions
- Ubuntu/Debian (apt)
- Fedora/CentOS/RHEL (dnf/yum)
- Arch (pacman)
- Alpine (apk)
- openSUSE (zypper)
- Gentoo (emerge)

### Special Cases
- Raspberry Pi (detected as raspbian, uses aarch64 binaries)
- Alpine Linux (uses musl-specific binaries)

## Testing

Before release:
1. Test installation on clean systems
2. Verify KECHO output formatting
3. Check PATH configuration
4. Validate script deployment
5. Test uninstall functionality