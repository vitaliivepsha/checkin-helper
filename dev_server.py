"""Standalone local launcher for the Mini App's web/API layer only - no
Telegram polling, no background sync loops (see start_webapp_server's own
`dev_mode` docstring for why those are skipped: they'd double up on the
same Untappd quota and GitHub repo the production VM already polls).

For testing a frontend/backend change against real Untappd data and a
real, HMAC-valid Telegram initData session before pushing it to the VM -
open it through an actual Telegram WebView via bot.py's owner-only
/dev_app command, which points at DEV_PUBLIC_BASE_URL (set that in the
VM's .env, not here).

Usage:
    python dev_server.py
    ngrok http <PORT>          # reuse the free static domain - see .env's
                                # own commented DEV_PUBLIC_BASE_URL line
    # then on the VM: set DEV_PUBLIC_BASE_URL to that same URL in .env,
    # restart the bot once, and run /dev_app there.
"""

import asyncio
import os

from dotenv import load_dotenv

load_dotenv()

import bot  # noqa: E402 - side effect only: loads ALL_BEERS/SESSIONS_RAW and sets up logging, does NOT start polling (guarded by bot.py's own `if __name__ == "__main__"`)
from webapp_server import start_webapp_server  # noqa: E402


class _DummyBot:
    async def send_message(self, *args, **kwargs) -> None:
        pass


class _DummyApp:
    bot = _DummyBot()
    bot_data: dict = {}


async def main() -> None:
    await start_webapp_server(
        _DummyApp(), bot.ALL_BEERS, bot.DATA_DIR, bot.SESSIONS_RAW, dev_mode=True,
        get_festival_data=bot.get_festival_data, active_festival_key=bot.ACTIVE_FESTIVAL_KEY,
    )
    port = os.environ.get("PORT", 8080)
    print(f"dev_server: listening on :{port} (dev_mode=True - no bot polling, no background loops)")
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
