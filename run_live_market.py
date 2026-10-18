from __future__ import annotations

from money_mintter.core.live_bot import LiveMarketBot


def main() -> None:
    bot = LiveMarketBot()
    bot.run()


if __name__ == "__main__":
    main()
