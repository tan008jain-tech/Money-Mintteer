from money_mintter.domain import WalletLedger


def test_full_profit_reinvestment() -> None:
    wallet = WalletLedger(100.0)
    pnl = wallet.apply_realized_pnl(10.0)
    assert pnl == 10.0
    assert wallet.wallet == 110.0
    assert wallet.realized_profit == 10.0


def test_loss_reduces_wallet() -> None:
    wallet = WalletLedger(100.0)
    wallet.apply_realized_pnl(-5.0)
    assert wallet.wallet == 95.0
    assert wallet.realized_loss == 5.0
