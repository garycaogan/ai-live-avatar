# /// script
# requires-python = ">=3.11"
# dependencies = ["aiohttp>=3.9"]
# ///
"""Run the console against the mock Vidu in one process, for UI work without spending credits.

    uv run tests/dev_mock_stack.py        # console on http://127.0.0.1:28900
"""
from __future__ import annotations

import asyncio
import copy
import sys
from pathlib import Path

from aiohttp import web

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from fixtures import ensure_default_avatars  # noqa: E402

ensure_default_avatars()
import server  # noqa: E402
from mock_vidu import API_KEY, build_mock  # noqa: E402


async def main(port: int = 28900, mock_port: int = 28991) -> None:
    mock = web.AppRunner(build_mock())
    await mock.setup()
    await web.TCPSite(mock, "127.0.0.1", mock_port).start()
    cfg = server.merge_profiles(copy.deepcopy(server.DEFAULTS))
    cfg["vidu"]["base_url"] = f"http://127.0.0.1:{mock_port}"
    evolink = server.read_key("EVOLINK_API_KEY", cfg["generator"]["api_key_file"])
    app = web.AppRunner(server.build_app(cfg, API_KEY, evolink, work_dir=ROOT / ".mock-run"))
    await app.setup()
    await web.TCPSite(app, "127.0.0.1", port).start()
    print(f"控制台 http://127.0.0.1:{port}/console （连的是模拟 Vidu，没有真实画面；生图{'开' if evolink else '关'}）", flush=True)
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
