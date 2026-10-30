from __future__ import annotations

import logging
import json
import os
import tempfile
import hashlib
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from typing import Any, Sequence

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.enums import DataFeed

from money_mintter.config import Settings
from money_mintter.calendar.service import ExchangeCalendarService, CalendarUnavailable


@dataclass(frozen=True, slots=True)
class SymbolSuitabilityMetrics:
    symbol: str
    latest_close: float
    median_close_5: float
    median_dollar_volume_20: float
    valid_bar_count: int
    atr_14: float
    atr_pct: float
    data_source: str
    status: str  # "eligible", "quarantined", "excluded"
    exclusion_reason: str


@dataclass(frozen=True, slots=True)
class SuitabilityResult:
    pool: list[str]
    quarantined: list[str]
    metrics: dict[str, SymbolSuitabilityMetrics]
    exclusion_counts: dict[str, int]
    requested_feed: str
    actual_feed: str
    data_source: str  # Alias for actual_feed for interface alignment
    generated_at: str
    stale: bool
    cached: bool
    config_signature: str
    universe_signature: str


class DailySuitabilityEngine:
    """Layer 2 Daily Suitability Filter for US Equities.
    Filters eligible Layer 1 symbols for price, history, liquidity, and volatility,
    placing extreme-volatility stocks in quarantine and ranking up to 200 core pool stocks.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        stock_client: StockHistoricalDataClient | None = None,
        cache_dir: str | None = None,
        logger: logging.Logger | None = None,
        historical_feed: str | None = None,
    ) -> None:
        self.settings = settings or Settings()
        self.stock_client = stock_client
        self.cache_dir = cache_dir or os.getenv(
            "MM_CACHE_DIR", os.getenv("MONEY_MINTTER_TEMP_DIR", os.path.join("data", "cache"))
        )
        self.cache_file = os.path.join(self.cache_dir, "daily_suitability_pool.json")
        self.logger = logger or logging.getLogger("MoneyMintterMasterBlueprint")
        self.historical_feed = (
            historical_feed
            or os.getenv("ALPACA_HISTORICAL_FEED")
            or getattr(self.settings, "alpaca_data_feed", "iex")
        ).strip().lower()

    def _get_stock_client(self) -> StockHistoricalDataClient:
        if self.stock_client is not None:
            return self.stock_client
        return StockHistoricalDataClient(
            api_key=self.settings.alpaca_api_key or "mock_key",
            secret_key=self.settings.alpaca_secret_key or "mock_secret",
        )

    def _compute_config_signature(self) -> str:
        cfg_str = (
            f"{self.settings.min_latest_close}|{self.settings.min_median_close_5}|"
            f"{self.settings.min_valid_daily_bars}|{self.settings.lookback_sessions}|"
            f"{self.settings.min_median_dollar_volume}|{self.settings.min_atr_pct}|"
            f"{self.settings.max_atr_pct}|{self.settings.max_daily_pool_size}|{self.historical_feed}"
        )
        return hashlib.sha256(cfg_str.encode("utf-8")).hexdigest()[:16]

    def _compute_universe_signature(self, symbols: list[str]) -> str:
        sorted_syms = sorted(symbols)
        univ_str = f"{len(sorted_syms)}:" + ",".join(sorted_syms[:10]) + (":" + sorted_syms[-1] if sorted_syms else "")
        return hashlib.sha256(univ_str.encode("utf-8")).hexdigest()[:16]

    def _is_session_expired(self, gen_dt: datetime, now_utc: datetime) -> bool:
        """Check if a new completed US trading session has occurred since gen_dt using market calendar."""
        try:
            calendar_service = ExchangeCalendarService({"US": "XNYS"})
            calendar = calendar_service._calendar("US")
            gen_date_str = gen_dt.date().isoformat()
            now_date_str = now_utc.date().isoformat()
            schedule = calendar.schedule.loc[gen_date_str:now_date_str]
            if schedule.empty:
                return gen_dt.date() != now_utc.date()

            completed_sessions = 0
            for _, row in schedule.iterrows():
                close_dt = row["close"].to_pydatetime().astimezone(timezone.utc)
                if close_dt <= now_utc and close_dt > gen_dt:
                    completed_sessions += 1
            return completed_sessions > 0
        except Exception:
            return gen_dt.date() != now_utc.date()

    def load_persisted_pool(self) -> SuitabilityResult | None:
        """Load previously persisted daily suitability pool and metrics if valid."""
        try:
            if os.path.exists(self.cache_file):
                with open(self.cache_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    return None
                pool = data.get("pool")
                generated_at = data.get("generated_at")
                if pool is None or generated_at is None or not isinstance(pool, list):
                    return None

                requested_feed = data.get("requested_feed")
                actual_feed = data.get("actual_feed")
                config_signature = data.get("config_signature")
                universe_signature = data.get("universe_signature")

                if not requested_feed or not isinstance(requested_feed, str) or not requested_feed.strip():
                    return None
                if not actual_feed or not isinstance(actual_feed, str) or not actual_feed.strip():
                    return None
                if not config_signature or not isinstance(config_signature, str) or not config_signature.strip():
                    return None
                if not universe_signature or not isinstance(universe_signature, str) or not universe_signature.strip():
                    return None

                data_source = data.get("data_source", actual_feed)
                quarantined = data.get("quarantined", [])
                raw_metrics = data.get("metrics", {})
                exclusion_counts = data.get("exclusion_counts", {})
                stale = bool(data.get("stale", False))

                metrics = {}
                for sym, m_dict in raw_metrics.items():
                    if isinstance(m_dict, dict):
                        metrics[sym] = SymbolSuitabilityMetrics(**m_dict)

                return SuitabilityResult(
                    pool=pool,
                    quarantined=quarantined,
                    metrics=metrics,
                    exclusion_counts=exclusion_counts,
                    requested_feed=requested_feed,
                    actual_feed=actual_feed,
                    data_source=data_source,
                    generated_at=generated_at,
                    stale=stale,
                    cached=True,
                    config_signature=config_signature,
                    universe_signature=universe_signature,
                )
        except Exception as exc:
            self.logger.warning(f"[Layer 2 Suitability] Failed to load persisted pool cache: {exc}")
        return None

    def load_stale_fallback_pool(
        self,
        eligible_symbols: list[str] | None = None,
        max_stale_sessions: int = 3,
    ) -> SuitabilityResult | None:
        """Load persisted pool for outage fallback, validating strict compatibility signatures
        (config signature, required input-universe signature, historical-feed policy & actual_feed) and
        trading-session-aware age policy. Fails closed if eligible_symbols is missing or incompatible.
        """
        if not eligible_symbols:
            self.logger.warning("[Layer 2 Suitability] Stale fallback rejected: eligible_symbols required for fail-closed verification.")
            return None

        res = self.load_persisted_pool()
        if not res or not res.generated_at or res.pool is None:
            return None

        # 1. Parse and validate generation timestamp independently
        try:
            gen_dt = datetime.fromisoformat(res.generated_at)
            if gen_dt.tzinfo is None:
                gen_dt = gen_dt.replace(tzinfo=timezone.utc)
        except Exception as exc:
            self.logger.warning(f"[Layer 2 Suitability] Stale fallback rejected: invalid timestamp format: {exc}")
            return None

        now_utc = datetime.now(timezone.utc)

        # 2. Validate configuration signature independently
        try:
            current_config_sig = self._compute_config_signature()
        except Exception as exc:
            self.logger.warning(f"[Layer 2 Suitability] Stale fallback rejected: config signature computation error: {exc}")
            return None

        if not res.config_signature or res.config_signature != current_config_sig:
            self.logger.warning("[Layer 2 Suitability] Stale fallback pool rejected: config signature mismatch or missing.")
            return None

        # 3. Validate input-universe signature independently (fail closed if missing or mismatch)
        try:
            current_universe_sig = self._compute_universe_signature(eligible_symbols)
        except Exception as exc:
            self.logger.warning(f"[Layer 2 Suitability] Stale fallback rejected: universe signature computation error: {exc}")
            return None

        if not res.universe_signature or res.universe_signature != current_universe_sig:
            self.logger.warning("[Layer 2 Suitability] Stale fallback pool rejected: universe signature mismatch or missing.")
            return None

        # 4. Validate historical-feed policy (requested_feed and actual_feed) independently
        feed_policy = self.historical_feed.strip().lower()
        req_feed = (res.requested_feed or "").strip().lower()
        act_feed = (res.actual_feed or res.data_source or "").strip().lower()
        if not req_feed or req_feed != feed_policy or not act_feed or act_feed != feed_policy:
            self.logger.warning(
                f"[Layer 2 Suitability] Stale fallback pool rejected: feed policy mismatch "
                f"(req={req_feed}, act={act_feed}, policy={feed_policy})."
            )
            return None

        # 5. Trading-session-aware age policy (calendar lookup) with calendar-specific exception fallback
        try:
            completed_sessions_count = 0
            calendar_service = ExchangeCalendarService({"US": "XNYS"})
            calendar = calendar_service._calendar("US")
            gen_date_str = gen_dt.date().isoformat()
            now_date_str = now_utc.date().isoformat()
            schedule = calendar.schedule.loc[gen_date_str:now_date_str]
            for _, row in schedule.iterrows():
                close_dt = row["close"].to_pydatetime().astimezone(timezone.utc)
                if close_dt > gen_dt and close_dt <= now_utc:
                    completed_sessions_count += 1

            if completed_sessions_count > max_stale_sessions:
                self.logger.warning(
                    f"[Layer 2 Suitability] Stale fallback pool rejected: completed sessions count "
                    f"({completed_sessions_count}) exceeds max stale sessions policy ({max_stale_sessions})."
                )
                return None
        except Exception as exc:
            self.logger.warning(f"[Layer 2 Suitability] Stale fallback calendar certainty loss / calendar error: {exc}")
            # Calendar-specific failure fallback: conservative 72-hour wall-clock limit
            age_hours = (now_utc - gen_dt).total_seconds() / 3600.0
            if age_hours > 72:
                self.logger.warning("[Layer 2 Suitability] Stale fallback rejected due to calendar certainty loss and age > 72 hours.")
                return None

        # Return with stale=True and original generated_at preserved
        return SuitabilityResult(
            pool=res.pool,
            quarantined=res.quarantined,
            metrics=res.metrics,
            exclusion_counts=res.exclusion_counts,
            requested_feed=res.requested_feed,
            actual_feed=res.actual_feed,
            data_source=res.data_source,
            generated_at=res.generated_at,
            stale=True,
            cached=False,
            config_signature=res.config_signature,
            universe_signature=res.universe_signature,
        )

    def save_persisted_pool(self, result: SuitabilityResult) -> None:
        """Atomic and interruption-safe pool cache saving."""
        try:
            os.makedirs(self.cache_dir, exist_ok=True)
            payload = {
                "pool": result.pool,
                "quarantined": result.quarantined,
                "metrics": {sym: asdict(m) for sym, m in result.metrics.items()},
                "exclusion_counts": result.exclusion_counts,
                "requested_feed": result.requested_feed,
                "actual_feed": result.actual_feed,
                "data_source": result.data_source,
                "generated_at": result.generated_at,
                "stale": result.stale,
                "config_signature": result.config_signature,
                "universe_signature": result.universe_signature,
            }
            fd, tmp_path = tempfile.mkstemp(dir=self.cache_dir, prefix="suitability_pool_", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(payload, f, indent=2)
                os.replace(tmp_path, self.cache_file)
            except Exception:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
                raise
        except Exception as exc:
            self.logger.warning(f"[Layer 2 Suitability] Failed to save persisted pool atomically: {exc}")

    def evaluate_universe(
        self,
        eligible_symbols: list[str],
        now_utc: datetime | None = None,
        force_refresh: bool = False,
    ) -> SuitabilityResult:
        """Evaluate eligible Layer 1 symbols against Layer 2 daily suitability rules."""
        now_utc = now_utc or datetime.now(timezone.utc)
        if now_utc.tzinfo is None:
            now_utc = now_utc.replace(tzinfo=timezone.utc)

        current_config_sig = self._compute_config_signature()
        current_universe_sig = self._compute_universe_signature(eligible_symbols)

        # Check cache validity & strict invalidation criteria
        if not force_refresh:
            cached_result = self.load_persisted_pool()
            if cached_result and cached_result.generated_at:
                try:
                    gen_dt = datetime.fromisoformat(cached_result.generated_at)
                    if gen_dt.tzinfo is None:
                        gen_dt = gen_dt.replace(tzinfo=timezone.utc)

                    session_expired = self._is_session_expired(gen_dt, now_utc)
                    config_matches = bool(cached_result.config_signature) and cached_result.config_signature == current_config_sig
                    universe_matches = bool(cached_result.universe_signature) and cached_result.universe_signature == current_universe_sig
                    feed_policy = self.historical_feed.strip().lower()
                    req_feed = (cached_result.requested_feed or "").strip().lower()
                    act_feed = (cached_result.actual_feed or cached_result.data_source or "").strip().lower()
                    feed_matches = bool(req_feed) and req_feed == feed_policy and bool(act_feed) and act_feed == feed_policy

                    if not session_expired and config_matches and universe_matches and feed_matches and not cached_result.stale:
                        self.logger.info(
                            f"[Layer 2 Suitability] Reusing cached daily pool (pool size: {len(cached_result.pool)}, "
                            f"stale: {cached_result.stale})"
                        )
                        return cached_result
                    else:
                        self.logger.info(
                            f"[Layer 2 Suitability] Cache invalidated due to change in (session_expired={session_expired}, "
                            f"config_matches={config_matches}, universe_matches={universe_matches}, feed_matches={feed_matches})."
                        )
                except Exception:
                    pass

        client = self._get_stock_client()
        lookback_days = max(45, self.settings.lookback_sessions * 3)
        start_dt = now_utc - timedelta(days=lookback_days)

        requested_feed_label = self.historical_feed.upper()
        target_feed_enum = DataFeed.SIP if self.historical_feed == "sip" else DataFeed.IEX

        feeds_to_try = [target_feed_enum]
        if target_feed_enum == DataFeed.SIP and os.getenv("ALPACA_ALLOW_IEX_FALLBACK", "false").lower() == "true":
            feeds_to_try.append(DataFeed.IEX)

        all_bars_data: dict[str, list[Any]] = {}
        batch_size = 50
        fetch_failed = True
        actual_feed_used = requested_feed_label

        for attempt_feed in feeds_to_try:
            current_feed_label = "SIP" if attempt_feed == DataFeed.SIP else "IEX"
            all_bars_data = {}
            current_fetch_failed = False

            for i in range(0, len(eligible_symbols), batch_size):
                batch = eligible_symbols[i : i + batch_size]
                if not batch:
                    continue

                try:
                    req = StockBarsRequest(
                        symbol_or_symbols=batch[0] if len(batch) == 1 else batch,
                        timeframe=TimeFrame(1, TimeFrameUnit.Day),
                        start=start_dt,
                        end=now_utc,
                        feed=attempt_feed,
                    )
                    resp = client.get_stock_bars(req)
                    resp_data = getattr(resp, "data", {}) or {}
                    for sym_key, bars in resp_data.items():
                        if isinstance(sym_key, (list, tuple)):
                            for s in sym_key:
                                all_bars_data.setdefault(str(s), []).extend(bars)
                        else:
                            all_bars_data.setdefault(str(sym_key), []).extend(bars)
                except Exception as batch_ex:
                    self.logger.warning(
                        f"[Layer 2 Suitability] Feed {current_feed_label} failed for batch {i//batch_size}: {batch_ex}"
                    )
                    current_fetch_failed = True
                    break

            if not current_fetch_failed and all_bars_data:
                fetch_failed = False
                actual_feed_used = current_feed_label
                break
            else:
                all_bars_data = {}

        # If fetch failed or encountered complete communication error across feeds/batches:
        if fetch_failed or (not all_bars_data and len(eligible_symbols) > 0):
            raise RuntimeError("UNIVERSE_DISCOVERY_FAILED: Layer 2 historical bar fetch failed and no valid live pool can be evaluated.")

        # Process symbols and evaluate rules
        metrics: dict[str, SymbolSuitabilityMetrics] = {}
        exclusion_counts = {
            "low_price": 0,
            "low_median_price": 0,
            "insufficient_history": 0,
            "low_liquidity": 0,
            "low_volatility": 0,
            "high_volatility_quarantine": 0,
            "no_bars": 0,
            "api_error": 0,
        }

        eligible_candidates: list[tuple[str, float, float]] = []
        quarantined_symbols: list[str] = []
        today_date = now_utc.date()

        for sym in eligible_symbols:
            raw_bars = all_bars_data.get(sym, [])
            if not raw_bars:
                exclusion_counts["no_bars"] += 1
                metrics[sym] = SymbolSuitabilityMetrics(
                    symbol=sym,
                    latest_close=0.0,
                    median_close_5=0.0,
                    median_dollar_volume_20=0.0,
                    valid_bar_count=0,
                    atr_14=0.0,
                    atr_pct=0.0,
                    data_source=actual_feed_used,
                    status="excluded",
                    exclusion_reason="no_bars",
                )
                continue

            completed_bars = []
            for b in raw_bars:
                b_dt = b.timestamp.replace(tzinfo=timezone.utc) if b.timestamp.tzinfo is None else b.timestamp
                if b_dt.date() < today_date:
                    completed_bars.append((b_dt, b))

            completed_bars.sort(key=lambda x: x[0])
            lookback_n = self.settings.lookback_sessions
            window_bars = [b for _, b in completed_bars[-lookback_n:]]

            valid_bar_count = len(window_bars)
            if valid_bar_count < self.settings.min_valid_daily_bars:
                exclusion_counts["insufficient_history"] += 1
                metrics[sym] = SymbolSuitabilityMetrics(
                    symbol=sym,
                    latest_close=float(window_bars[-1].close) if window_bars else 0.0,
                    median_close_5=0.0,
                    median_dollar_volume_20=0.0,
                    valid_bar_count=valid_bar_count,
                    atr_14=0.0,
                    atr_pct=0.0,
                    data_source=actual_feed_used,
                    status="excluded",
                    exclusion_reason="insufficient_history",
                )
                continue

            closes = [float(b.close) for b in window_bars]
            latest_close = closes[-1]

            if latest_close < self.settings.min_latest_close:
                exclusion_counts["low_price"] += 1
                metrics[sym] = SymbolSuitabilityMetrics(
                    symbol=sym,
                    latest_close=latest_close,
                    median_close_5=0.0,
                    median_dollar_volume_20=0.0,
                    valid_bar_count=valid_bar_count,
                    atr_14=0.0,
                    atr_pct=0.0,
                    data_source=actual_feed_used,
                    status="excluded",
                    exclusion_reason="low_price",
                )
                continue

            recent_5_closes = closes[-5:] if len(closes) >= 5 else closes
            sorted_5 = sorted(recent_5_closes)
            mid_idx = len(sorted_5) // 2
            median_close_5 = sorted_5[mid_idx] if len(sorted_5) % 2 != 0 else (sorted_5[mid_idx - 1] + sorted_5[mid_idx]) / 2.0

            if median_close_5 < self.settings.min_median_close_5:
                exclusion_counts["low_median_price"] += 1
                metrics[sym] = SymbolSuitabilityMetrics(
                    symbol=sym,
                    latest_close=latest_close,
                    median_close_5=median_close_5,
                    median_dollar_volume_20=0.0,
                    valid_bar_count=valid_bar_count,
                    atr_14=0.0,
                    atr_pct=0.0,
                    data_source=actual_feed_used,
                    status="excluded",
                    exclusion_reason="low_median_price",
                )
                continue

            dollar_volumes = []
            for b in window_bars:
                c = float(b.close)
                v = float(b.volume)
                vwap = float(b.vwap) if hasattr(b, "vwap") and b.vwap is not None else 0.0
                price_to_use = vwap if vwap > 0 else c
                dollar_volumes.append(price_to_use * v)

            sorted_dv = sorted(dollar_volumes)
            dv_mid = len(sorted_dv) // 2
            median_dv = sorted_dv[dv_mid] if len(sorted_dv) % 2 != 0 else (sorted_dv[dv_mid - 1] + sorted_dv[dv_mid]) / 2.0

            if median_dv < self.settings.min_median_dollar_volume:
                exclusion_counts["low_liquidity"] += 1
                metrics[sym] = SymbolSuitabilityMetrics(
                    symbol=sym,
                    latest_close=latest_close,
                    median_close_5=median_close_5,
                    median_dollar_volume_20=median_dv,
                    valid_bar_count=valid_bar_count,
                    atr_14=0.0,
                    atr_pct=0.0,
                    data_source=actual_feed_used,
                    status="excluded",
                    exclusion_reason="low_liquidity",
                )
                continue

            tr_list = []
            prev_c = None
            for b in window_bars:
                h = float(b.high)
                l = float(b.low)
                c = float(b.close)
                if prev_c is None:
                    tr = h - l
                else:
                    tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
                tr_list.append(tr)
                prev_c = c

            atr_period = 14
            if len(tr_list) >= atr_period:
                initial_atr = sum(tr_list[:atr_period]) / atr_period
                current_atr = initial_atr
                for tr_val in tr_list[atr_period:]:
                    current_atr = (current_atr * (atr_period - 1) + tr_val) / atr_period
                atr_14 = current_atr
            else:
                atr_14 = sum(tr_list) / len(tr_list) if tr_list else 0.0

            atr_pct = atr_14 / latest_close if latest_close > 0 else 0.0

            if atr_pct < self.settings.min_atr_pct:
                exclusion_counts["low_volatility"] += 1
                metrics[sym] = SymbolSuitabilityMetrics(
                    symbol=sym,
                    latest_close=latest_close,
                    median_close_5=median_close_5,
                    median_dollar_volume_20=median_dv,
                    valid_bar_count=valid_bar_count,
                    atr_14=atr_14,
                    atr_pct=atr_pct,
                    data_source=actual_feed_used,
                    status="excluded",
                    exclusion_reason="low_volatility",
                )
                continue

            if atr_pct > self.settings.max_atr_pct:
                exclusion_counts["high_volatility_quarantine"] += 1
                quarantined_symbols.append(sym)
                metrics[sym] = SymbolSuitabilityMetrics(
                    symbol=sym,
                    latest_close=latest_close,
                    median_close_5=median_close_5,
                    median_dollar_volume_20=median_dv,
                    valid_bar_count=valid_bar_count,
                    atr_14=atr_14,
                    atr_pct=atr_pct,
                    data_source=actual_feed_used,
                    status="quarantined",
                    exclusion_reason="high_volatility_quarantine",
                )
                continue

            liq_score = min(1.0, median_dv / 200_000_000.0)
            vol_score = min(1.0, atr_pct / 0.05)
            suitability_score = 0.65 * liq_score + 0.35 * vol_score

            eligible_candidates.append((sym, suitability_score, median_dv))
            metrics[sym] = SymbolSuitabilityMetrics(
                symbol=sym,
                latest_close=latest_close,
                median_close_5=median_close_5,
                median_dollar_volume_20=median_dv,
                valid_bar_count=valid_bar_count,
                atr_14=atr_14,
                atr_pct=atr_pct,
                data_source=actual_feed_used,
                status="eligible",
                exclusion_reason="",
            )

        eligible_candidates.sort(key=lambda item: (-item[1], item[0]))
        max_pool = self.settings.max_daily_pool_size
        core_pool = [sym for sym, _, _ in eligible_candidates[:max_pool]]
        quarantined_symbols.sort()

        generated_at = now_utc.isoformat()
        result = SuitabilityResult(
            pool=core_pool,
            quarantined=quarantined_symbols,
            metrics=metrics,
            exclusion_counts=exclusion_counts,
            requested_feed=requested_feed_label,
            actual_feed=actual_feed_used,
            data_source=actual_feed_used,
            generated_at=generated_at,
            stale=False,
            cached=False,
            config_signature=current_config_sig,
            universe_signature=current_universe_sig,
        )

        self.save_persisted_pool(result)
        self.logger.info(
            f"[Layer 2 Suitability] Evaluated {len(eligible_symbols)} symbols | "
            f"Core Pool: {len(core_pool)} | Quarantined: {len(quarantined_symbols)} | "
            f"Exclusions: {exclusion_counts} | Feed: {actual_feed_used} (Requested: {requested_feed_label})"
        )
        return result
