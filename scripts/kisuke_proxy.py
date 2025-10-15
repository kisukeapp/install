"""Entry point for the modular Kisuke proxy."""

from __future__ import annotations

import asyncio
import os

from proxy.app import start_proxy


async def _main() -> None:
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8082"))
    runner = await start_proxy(host, port)
    print(f"[proxy] listening on http://{host}:{port}")
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(_main())
