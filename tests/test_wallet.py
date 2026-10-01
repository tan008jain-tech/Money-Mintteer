from money_mintter.domain import WalletLedger


def test_profit_reserve_split() -> None:
    wallet = WalletLedger(100.0, profit_reserve_pct=0.20)
    reinvested, reserved = wallet.apply_realized_pnl(10.0)
    assert reinvested == 8.0
    assert reserved == 2.0
    assert wallet.trading_wallet == 108.0
    assert wallet.profit_reserve == 2.0
    assert wallet.total_tracked_capital == 110.0


def test_loss_stays_in_trading_wallet() -> None:
    wallet = WalletLedger(100.0, profit_reserve_pct=0.20)
    wallet.apply_realized_pnl(-5.0)
    assert wallet.trading_wallet == 95.0
    assert wallet.profit_reserve == 0.0
