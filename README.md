# Kisuke Installation Scripts

Part of [Kisuke](https://kisuke.dev) - Your favorite pocket engineer.

## What is this?

These scripts are automatically deployed by the Kisuke iOS app to set up your development environment on Mac, Linux, and remote servers. They handle the installation of essential tools like Python, Node.js, tmux, and Claude Code CLI.

## Quick Start

When Kisuke deploys these scripts to your system, you can run:

```bash
# Check system status
bash setup.sh

# Install all tools
bash setup.sh --install

# Deploy Kisuke runtime scripts
bash setup.sh --deploy

# Install specific tools
bash setup.sh --install nodejs python3
```

## What gets installed?

- **Development Tools**: Python 3.12, Node.js 22.9, jq 1.7
- **Terminal Tools**: tmux, git
- **AI Tools**: Claude Code CLI & SDK
- **Kisuke Scripts**: Runtime scripts for iOS app integration

All tools are installed to `~/.kisuke/` to keep your system clean.

## Supported Platforms

- macOS (Intel & Apple Silicon)
- Linux (Ubuntu, Debian, Fedora, Arch, Alpine, and more)
- Raspberry Pi
- Remote servers

## License

These scripts are licensed for use with the Kisuke iOS application only. See [LICENSE](LICENSE) for details.

## Support

For help or questions: [@0xkyon](https://x.com/0xkyon)