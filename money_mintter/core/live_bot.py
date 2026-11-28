        def _safe_flatten(exit_reason: str = "eod_flatten") -> list[str]:

            def _record_flatten_error() -> None:
                try:
                    self.portfolio_manager.last_flatten_errors = (
                        getattr(self.portfolio_manager, "last_flatten_errors", 0) + 1
                    )
                except Exception:
                    self.logger.error("Unable to increment flatten error counter.")

            try:
                res_pos = self.portfolio_manager.flatten_all(
                    sleep_fn,
                    max_wait_seconds=self.settings.flatten_max_wait_seconds,
                    exit_reason=exit_reason,
                )
            except Exception as exc:
                _record_flatten_error()
                self.logger.critical(f"flatten_all raised exception: {exc}")
                res_pos = ["UNKNOWN"]

            if res_pos:
                if res_pos == ["UNKNOWN"]:
                    self.logger.critical(
                        f"POSITIONS MAY STILL BE OPEN: {res_pos} "
                        "(position state could not be verified; broker order state may also be unresolved)"
                    )
                else:
                    self.logger.critical(f"POSITIONS MAY STILL BE OPEN: {res_pos}")
                return res_pos

            # Take a fresh account/positions/orders snapshot after liquidation.
            # The last normal cycle's equity is not the final session equity.
            try:
                (
                    final_cash,
                    final_equity,
                    final_buying_power,
                    final_positions,
                    final_dashboard_positions,
                    final_locked_symbols,
                ) = self.portfolio_manager.fetch_account_and_positions()
                final_open_order_symbols = self.portfolio_manager.fetch_open_order_symbols()

                self.latest_state["cash"] = final_cash
                self.latest_state["equity"] = final_equity
                self.latest_state["open_positions"] = final_dashboard_positions

                self.logger.info(
                    f"[Final Broker Snapshot] equity=${final_equity:.2f} "
                    f"cash=${final_cash:.2f} buying_power=${final_buying_power:.2f} "
                    f"account_positions={len(final_positions)} "
                    f"account_open_order_symbols={len(final_open_order_symbols)}"
                )

                remaining_symbols = {
                    str(position.get("symbol") or "").strip().upper()
                    for position in final_positions
                    if isinstance(position, dict) and position.get("symbol")
                }
                if any(
                    not isinstance(position, dict) or not str(position.get("symbol") or "").strip()
                    for position in final_positions
                ):
                    remaining_symbols.add("UNKNOWN")
                remaining_symbols.update(final_open_order_symbols)
                if remaining_symbols:
                    remaining = sorted(remaining_symbols)
                    _record_flatten_error()
                    self.latest_state["final_account_snapshot_verified"] = False
                    self.logger.critical(
                        "[Final Broker Snapshot] Alpaca still reports positions/open orders "
                        f"after flatten: {remaining}"
                    )
                    return remaining

                self.latest_state["final_account_snapshot_verified"] = True
            except Exception as exc:
                _record_flatten_error()
                self.logger.critical(
                    f"Unable to verify final broker account/positions/orders snapshot: {exc}"
                )
                return ["UNKNOWN"]

            return []

        try:
            try:
                clock = self.trading_client.get_clock()
            except Exception as exc:
                self.logger.error(f"Failed to get market clock: {exc}")
                return {"status": "error", "cycles": 0, "orders_sent": 0, "errors": 1, "remaining": []}

            is_open = getattr(clock, 'is_open', False)
            next_open = getattr(clock, 'next_open', None)
            next_close = getattr(clock, 'next_close', None)

            now = now_fn()
            if self._session_start_utc is None:
                if not isinstance(now, datetime):
                    raise ValueError("session clock must return a datetime")
                session_start = now
                if session_start.tzinfo is None or session_start.utcoffset() is None:
                    session_start = session_start.replace(tzinfo=timezone.utc)
                self._session_start_utc = session_start.astimezone(timezone.utc)

            if not is_open:
                if next_open is not None:
                    if next_open.tzinfo is None:
                        next_open = next_open.replace(tzinfo=timezone.utc)
                    if now.tzinfo is None:
                        now = now.replace(tzinfo=timezone.utc)
                    wait_mins = (next_open - now).total_seconds() / 60.0
                    if wait_mins > self.settings.max_wait_for_open_minutes:
                        self.logger.info(f"Market closed and next_open ({next_open.isoformat()}) is {wait_mins:.1f} mins away (> max_wait={self.settings.max_wait_for_open_minutes} mins). Skipping session.")
                        return {"status": "skipped_market_closed", "cycles": 0, "orders_sent": 0, "errors": 0, "remaining": []}
                    else:
                        self.logger.info(f"Market closed. Waiting until next open at {next_open.isoformat()} ({wait_mins:.1f} mins)...")
                        _sleep_until(next_open, now_fn, sleep_fn)
                        try:
                            clock = self.trading_client.get_clock()
                            is_open = getattr(clock, 'is_open', False)
                            next_open = getattr(clock, 'next_open', next_open)
                            next_close = getattr(clock, 'next_close', next_close)
                        except Exception as exc:
                            self.logger.error(f"Failed to get clock after waiting for open: {exc}")

                        if not getattr(clock, 'is_open', False):
                            self.logger.info("Market still not open after waiting. Skipping session.")
                            return {"status": "skipped_market_closed", "cycles": 0, "orders_sent": 0, "errors": 0, "remaining": []}
                else:
                    return {"status": "skipped_market_closed", "cycles": 0, "orders_sent": 0, "errors": 0, "remaining": []}

            if next_close is not None and next_close.tzinfo is None:
                next_close = next_close.replace(tzinfo=timezone.utc)

            close_time = next_close
            flatten_mins = self.settings.flatten_minutes_before_close
            no_new_entries_mins = self.settings.no_new_entries_minutes_before_close

            last_known_close = close_time

            while True:
                now = now_fn()

                try:
                    clk = self.trading_client.get_clock()
                    if hasattr(clk, 'next_close') and clk.next_close is not None:
                        nc = clk.next_close
                        if nc.tzinfo is None:
                            nc = nc.replace(tzinfo=timezone.utc)
                        last_known_close = nc
                except Exception as exc:
                    self.logger.warning(f"Failed to re-fetch clock, using last known close {last_known_close}: {exc}")

                close_time = last_known_close
                flatten_at = close_time - timedelta(minutes=flatten_mins)
                cutoff = close_time - timedelta(minutes=no_new_entries_mins)

                if self.kill_switch.tripped:
                    self.logger.critical(f"Kill switch tripped mid-session: {self.kill_switch.reason}")
                    remaining = _safe_flatten("emergency_cleanup")
                    status = "halted" if remaining != ["UNKNOWN"] else "flatten_incomplete"
                    self._log_session_summary(status, cycles, orders_sent_total, errors)
                    return {"status": status, "cycles": cycles, "orders_sent": orders_sent_total, "errors": errors, "remaining": remaining}

                if now >= flatten_at:
                    break

                if now < cutoff:
                    trading_started = True
                    res = self.run_once(now)
                    cycles += 1
                    orders_sent_total += res.get("orders_sent", 0)
                    if self.kill_switch.tripped or res.get("halted", False):
                        self.logger.critical(f"Kill switch tripped: {self.kill_switch.reason if hasattr(self.kill_switch, 'reason') else 'halted'}")
                        remaining = _safe_flatten("emergency_cleanup")
                        status = "halted" if remaining != ["UNKNOWN"] else "flatten_incomplete"
                        self._log_session_summary(status, cycles, orders_sent_total, errors)
                        return {"status": status, "cycles": cycles, "orders_sent": orders_sent_total, "errors": errors, "remaining": remaining}
                    if res.get("error") or res.get("outage"):
                        errors += 1
                        sleep_fn(10)
                        continue

                nb = next_bar_boundary(now)
                target = min(nb, flatten_at)
                if target > now:
                    _sleep_until(target, now_fn, sleep_fn)

            remaining = _safe_flatten("eod_flatten")
            status = "completed" if not remaining and remaining != ["UNKNOWN"] else "flatten_incomplete"
            if remaining == ["UNKNOWN"]:
                status = "flatten_incomplete"
            self._log_session_summary(status, cycles, orders_sent_total, errors)
            return {"status": status, "cycles": cycles, "orders_sent": orders_sent_total, "errors": errors, "remaining": remaining}

        except KeyboardInterrupt:
            self.logger.warning("Session interrupted by user (KeyboardInterrupt).")
            remaining = []
            if trading_started:
                remaining = _safe_flatten("emergency_cleanup")
            status = "interrupted" if remaining != ["UNKNOWN"] else "flatten_incomplete"
            self._log_session_summary(status, cycles, orders_sent_total, errors)
            return {"status": status, "cycles": cycles, "orders_sent": orders_sent_total, "errors": errors, "remaining": remaining}

        except Exception as exc:
            self.logger.exception(f"Session error: {exc}")
            errors += 1
            remaining = []
            if trading_started:
                remaining = _safe_flatten("emergency_cleanup")
            status = "error" if remaining != ["UNKNOWN"] else "flatten_incomplete"
            self._log_session_summary(status, cycles, orders_sent_total, errors)
            return {"status": status, "cycles": cycles, "orders_sent": orders_sent_total, "errors": errors, "remaining": remaining}

    def _log_session_summary(self, status: str, cycles: int, orders_sent: int, errors: int) -> None:
        flatten_errors = getattr(self.portfolio_manager, "last_flatten_errors", 0)
        snapshot_verified = bool(
            self.latest_state.get("final_account_snapshot_verified", False)
        )
        snapshot_label = "verified" if snapshot_verified else "unverified"

        # Reconcile fills and account FEE activities once more after EOD flatten.
        # This section is deliberately separate from broker account equity, which
        # remains the authoritative account-level result.
        journal = getattr(self.portfolio_manager, "order_journal", None)
        session_start = getattr(self, "_session_start_utc", None)
        if journal is not None and isinstance(session_start, datetime):
            report_end = datetime.now(timezone.utc)
            if session_start.tzinfo is None or session_start.utcoffset() is None:
                session_start = session_start.replace(tzinfo=timezone.utc)
            session_start = session_start.astimezone(timezone.utc)
            if report_end >= session_start:
                try:
                    reconciliation_ok = self.portfolio_manager.reconcile_order_journal()
                except Exception as exc:
                    reconciliation_ok = False
                    self.logger.warning(f"[Order Journal] Final fill reconciliation failed: {exc}")
                try:
                    self.portfolio_manager.reconcile_fee_activities(session_start, report_end)
                except Exception as exc:
                    self.logger.warning(f"[Order Journal] Final fee activity reconciliation failed: {exc}")
                try:
                    metrics = journal.summarize_session(session_start, report_end)
                    fee_amount = metrics["fee_activity_net_amount_usd"]
                    fee_label = (
                        f"${fee_amount:+.2f}" if fee_amount is not None else "not yet reported"
                    )
                    self.logger.info(
                        f"[Trade P&L] window={session_start.isoformat()}..{report_end.isoformat()} "
                        f"realized_gross=${metrics['realized_gross_pnl_usd']:+.2f} "
                        f"matched_exit_qty={metrics['matched_exit_quantity']:.4f} "
                        f"unmatched_exit_qty={metrics['unmatched_exit_quantity']:.4f} "
                        f"open_entry_qty_after_window={metrics['open_entry_quantity_after_session']:.4f} "
                        f"unattributed_fills={metrics['unattributed_fill_count']} "
                        f"closing_fills={metrics['closing_fill_count']} "
                        f"session_fills={metrics['fill_count_in_session']} "
                        f"coverage={metrics['pnl_coverage_status']} "
                        f"account_fee_activity_net={fee_label} "
                        f"fee_activity_count={metrics['fee_activity_count']} "
                        f"fee_activity_as_of={report_end.isoformat()} "
                        f"journal_reconciliation={'healthy' if reconciliation_ok else 'incomplete'} "
                        "net_pnl=NOT_CALCULATED"
                    )
                except Exception as exc:
                    self.logger.warning(f"[Trade P&L] Session fill summary unavailable: {exc}")

        if cycles == 0 and not snapshot_verified:
            self.logger.info(
                f"[Session Summary] status={status}, cycles={cycles}, orders_sent={orders_sent}, "
                f"cycle_errors={errors}, flatten_errors={flatten_errors}, "
                f"final_account_snapshot={snapshot_label}, equity=n/a"
            )
            return

        equity = self.latest_state.get("equity")
        last_eq = self.portfolio_manager.last_equity
        change_str = ""
        if equity is not None:
            equity_value = float(equity)
            if last_eq is not None and last_eq > 0:
                diff = equity_value - last_eq
                pct = (diff / last_eq) * 100.0
                change_str = (
                    f", equity=${equity_value:.2f}, change=${diff:+.2f} ({pct:+.2f}%) "
                    f"vs last_equity=${last_eq:.2f}"
                )
            else:
                change_str = f", equity=${equity_value:.2f}"
        else:
            change_str = ", equity=n/a"

        self.logger.info(
            f"[Session Summary] status={status}, cycles={cycles}, orders_sent={orders_sent}, "
            f"cycle_errors={errors}, flatten_errors={flatten_errors}, "
            f"final_account_snapshot={snapshot_label}{change_str}"
        )


def _sleep_until(target_dt: datetime, now_fn, sleep_fn) -> None:
    while True:
        now = now_fn()
        if now.tzinfo is None and target_dt.tzinfo is not None:
            now = now.replace(tzinfo=timezone.utc)
        elif now.tzinfo is not None and target_dt.tzinfo is None:
            target_dt = target_dt.replace(tzinfo=timezone.utc)
        diff = (target_dt - now).total_seconds()
        if diff <= 0:
            break
        chunk = min(60.0, diff)
        sleep_fn(chunk)
        if now_fn() == now:
            break


def main() -> int:
    print("==================================================")
    print(" PAPER TRADING SESSION MODE ACTIVE                ")
    print("==================================================")
    lock_path = os.path.join(os.getenv("MM_LOG_DIR", "logs"), "mm_session.lock")
    lock = SessionLock(lock_path)
    if not lock.acquire():
        print(f"Session lock acquisition failed (lock file exists and is fresh): {lock_path}")
        return 4

    try:
        bot = LiveMarketBot()
        res = bot.run_session()
        status = res.get("status")
        remaining = res.get("remaining", [])
        if remaining:
            print("==================================================")
            print(f" POSITIONS MAY STILL BE OPEN: {remaining}")
            print(" CHECK ALPACA AND CLOSE THEM MANUALLY")
            print("==================================================")

        if status in ("completed", "skipped_market_closed"):
            return 0
        elif status == "halted":
            return 2
        elif status == "flatten_incomplete":
            return 3
        elif status == "error":
            return 3
        elif status == "interrupted":
            return 130
        else:
            return 3
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
