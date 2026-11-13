from __future__ import annotations

import logging
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any

from alpaca.trading.enums import AssetStatus, AssetClass


@dataclass(frozen=True, slots=True)
class AssetMetadata:
    symbol: str
    name: str
    exchange: str
    shortable: bool
    fractionable: bool
    marginable: bool
    asset_class: str
    status: str
    tradable: bool


class StructuralEligibilityEngine:
    """Layer 1 Structural Eligibility Engine for US Equities."""

    def __init__(self, cache_dir: str | None = None, refresh_interval_hours: float = 24.0) -> None:
        self.cache_dir = cache_dir or os.getenv("MM_CACHE_DIR", os.getenv("MONEY_MINTTER_TEMP_DIR", os.path.join("data", "cache")))
        self.refresh_interval_hours = refresh_interval_hours
        self.cache_file = os.path.join(self.cache_dir, "structural_universe_cache.json")

    def evaluate_asset(self, asset: Any) -> tuple[bool, str, dict[str, Any] | None]:
        """Evaluate an asset against Layer 1 structural eligibility rules using safe, precise classification.
        Returns (is_eligible, exclusion_reason, metadata_dict).
        """
        if isinstance(asset, dict):
            symbol = str(asset.get("symbol", "")).upper().strip()
            status = str(asset.get("status", "")).lower()
            tradable = bool(asset.get("tradable", False))
            asset_class = str(asset.get("asset_class") or asset.get("class", "")).lower()
            exchange = str(asset.get("exchange", "")).upper().strip()
            name = str(asset.get("name", "")).strip()
            shortable = bool(asset.get("shortable", False))
            fractionable = bool(asset.get("fractionable", False))
            marginable = bool(asset.get("marginable", False))
        else:
            symbol = str(getattr(asset, "symbol", "")).upper().strip()
            status_val = getattr(asset, "status", "")
            status = str(status_val.value if hasattr(status_val, "value") else status_val).lower()
            tradable = bool(getattr(asset, "tradable", False))
            ac_val = getattr(asset, "asset_class", None)
            if ac_val is None and hasattr(asset, "class"):
                ac_val = getattr(asset, "class", "")
            asset_class = str(ac_val.value if hasattr(ac_val, "value") else ac_val).lower()
            exchange = str(getattr(asset, "exchange", "")).upper().strip()
            name = str(getattr(asset, "name", "")).strip()
            shortable = bool(getattr(asset, "shortable", False))
            fractionable = bool(getattr(asset, "fractionable", False))
            marginable = bool(getattr(asset, "marginable", False))

        if not symbol:
            return False, "missing_symbol", None

        if "/" in symbol:
            return False, "slash_symbol", None

        if status not in ("active", str(AssetStatus.ACTIVE).lower()):
            return False, "inactive", None

        if not tradable:
            return False, "non_tradable", None

        if asset_class not in ("us_equity", "us_equities", str(AssetClass.US_EQUITY).lower()):
            return False, "non_us_equity", None

        if exchange in ("OTC", "PINK", "PINK SHEETS", "OTCMKTS", "GREY", "OTCQX", "OTCQB"):
            return False, "otc_exchange", None

        # Safer instrument classification using precise word tokens / patterns to avoid false positives
        name_lower = name.lower()
        words = set(name_lower.split())

        # Warrants: standalone word "warrant" or suffix ".wt" / "-wt"
        if "warrant" in words or symbol.endswith(".WT") or symbol.endswith("-WT"):
            return False, "unsupported_instrument_warrant", None

        # Rights: standalone words "right" / "rights" or suffix ".rt" / "-rt"
        if "right" in words or "rights" in words or symbol.endswith(".RT") or symbol.endswith("-RT"):
            return False, "unsupported_instrument_right", None

        # Units: standalone word "unit" / "units" or suffix ".u" / "-u"
        if "unit" in words or "units" in words or symbol.endswith(".U") or symbol.endswith("-U"):
            return False, "unsupported_instrument_unit", None

        # Preferred shares: "preferred" / "pref" in words or ".pr" / "-pr" in symbol
        if "preferred" in words or "pref" in words or ".PR" in symbol or "-PR" in symbol:
            return False, "unsupported_instrument_preferred", None

        # Known leveraged or inverse ETFs: check precise patterns
        leveraged_phrases = [
            "2x", "3x", "ultrapro", "ultrashort", "ultra short",
            "inverse etf", "leveraged etf", "bear etf", "short etf"
        ]
        for phrase in leveraged_phrases:
            if phrase in name_lower:
                return False, f"unsupported_instrument_leveraged_{phrase.replace(' ', '_')}", None

        metadata = {
            "symbol": symbol,
            "name": name,
            "exchange": exchange,
            "shortable": shortable,
            "fractionable": fractionable,
            "marginable": marginable,
            "asset_class": asset_class,
            "status": status,
            "tradable": tradable,
        }
        return True, "eligible", metadata

    def filter_universe(self, assets: list[Any], logger: logging.Logger) -> tuple[list[str], dict[str, dict[str, Any]], dict[str, int]]:
        total_assets = len(assets)
        eligible_symbols = []
        metadata_map = {}
        exclusion_counts = {
            "inactive": 0,
            "non_tradable": 0,
            "non_us_equity": 0,
            "otc_exchange": 0,
            "unsupported_instrument": 0,
            "slash_symbol": 0,
            "missing_symbol": 0,
            "duplicate": 0,
        }
        seen_symbols = set()

        for asset in assets:
            is_elig, reason, meta = self.evaluate_asset(asset)
            if not is_elig:
                if reason in exclusion_counts:
                    exclusion_counts[reason] += 1
                else:
                    base_reason = reason.split("_")[0] if "_" in reason else reason
                    if base_reason in exclusion_counts:
                        exclusion_counts[base_reason] += 1
                    else:
                        exclusion_counts["unsupported_instrument"] += 1
                continue

            sym = meta["symbol"]
            if sym in seen_symbols:
                exclusion_counts["duplicate"] += 1
                continue
            seen_symbols.add(sym)
            eligible_symbols.append(sym)
            metadata_map[sym] = meta

        eligible_symbols.sort()
        logger.info(
            f"[Universe Layer 1] Total raw assets: {total_assets} | Eligible: {len(eligible_symbols)} | "
            f"Exclusions: {exclusion_counts}"
        )
        return eligible_symbols, metadata_map, exclusion_counts

    def load_persisted_cache(self) -> tuple[list[str], dict[str, dict[str, Any]], datetime | None]:
        """Validate and load persisted cache schema, symbols, metadata, and timestamp."""
        try:
            if os.path.exists(self.cache_file):
                with open(self.cache_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    return [], {}, None
                symbols = data.get("symbols", [])
                metadata = data.get("metadata", {})
                fetched_str = data.get("fetched_at")

                if not isinstance(symbols, list) or not isinstance(metadata, dict):
                    return [], {}, None

                fetched_at = None
                if fetched_str:
                    try:
                        fetched_at = datetime.fromisoformat(fetched_str)
                        if fetched_at.tzinfo is None:
                            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
                    except Exception:
                        fetched_at = None

                if symbols:
                    return symbols, metadata, fetched_at
        except Exception as exc:
            logging.getLogger("MoneyMintterMasterBlueprint").warning(f"[Universe Layer 1] Failed to load/validate persisted cache: {exc}")
        return [], {}, None

    def load_validated_cache(
        self,
        now_utc: datetime | None = None,
        max_age_hours: float = 24.0,
        min_symbols: int = 1,
    ) -> tuple[list[str], dict[str, dict[str, Any]], datetime | None]:
        """Load a recent Layer 1 cache and revalidate each symbol's structural metadata."""
        logger = logging.getLogger("MoneyMintterMasterBlueprint")
        now_utc = now_utc or datetime.now(timezone.utc)
        if now_utc.tzinfo is None:
            now_utc = now_utc.replace(tzinfo=timezone.utc)
        symbols, metadata, fetched_at = self.load_persisted_cache()
        if not symbols or fetched_at is None:
            logger.warning("[Universe Layer 1] Cache rejected: missing symbols or fetched_at timestamp.")
            return [], {}, None
        if max_age_hours <= 0 or min_symbols <= 0:
            logger.warning("[Universe Layer 1] Cache rejected: invalid age or minimum-size policy.")
            return [], {}, None
        age_hours = (now_utc - fetched_at).total_seconds() / 3600.0
        if age_hours < -0.0833 or age_hours > max_age_hours:
            logger.warning(
                f"[Universe Layer 1] Cache rejected: age={age_hours:.2f}h outside [0,{max_age_hours:.2f}]h."
            )
            return [], {}, None
        if len(symbols) < min_symbols:
            logger.warning(
                f"[Universe Layer 1] Cache rejected: {len(symbols)} symbols below health floor {min_symbols}."
            )
            return [], {}, None

        validated_symbols: list[str] = []
        validated_metadata: dict[str, dict[str, Any]] = {}
        seen: set[str] = set()
        for raw_symbol in symbols:
            if not isinstance(raw_symbol, str):
                logger.warning("[Universe Layer 1] Cache rejected: non-string symbol found.")
                return [], {}, None
            symbol = raw_symbol.upper().strip()
            if not symbol or symbol != raw_symbol or symbol in seen or "/" in symbol or any(ch.isspace() for ch in symbol):
                logger.warning(f"[Universe Layer 1] Cache rejected: invalid or duplicate symbol {raw_symbol!r}.")
                return [], {}, None
            cached_meta = metadata.get(symbol)
            if not isinstance(cached_meta, dict):
                logger.warning(f"[Universe Layer 1] Cache rejected: structural metadata missing for {symbol}.")
                return [], {}, None
            eligible, reason, normalized_meta = self.evaluate_asset(cached_meta)
            if not eligible or not normalized_meta or normalized_meta.get("symbol") != symbol:
                logger.warning(
                    f"[Universe Layer 1] Cache rejected: {symbol} failed structural revalidation ({reason})."
                )
                return [], {}, None
            seen.add(symbol)
            validated_symbols.append(symbol)
            validated_metadata[symbol] = normalized_meta

        validated_symbols.sort()
        logger.info(
            f"[Universe Layer 1] Revalidated persisted cache: {len(validated_symbols)} symbols, "
            f"age={age_hours:.2f}h, max_age={max_age_hours:.2f}h."
        )
        return validated_symbols, validated_metadata, fetched_at

    def save_persisted_cache(self, symbols: list[str], metadata: dict[str, dict[str, Any]], fetched_at: datetime) -> None:
        """Atomic and interruption-safe cache replacement using temporary file rename."""
        try:
            os.makedirs(self.cache_dir, exist_ok=True)
            data = {
                "symbols": symbols,
                "metadata": metadata,
                "fetched_at": fetched_at.isoformat(),
            }
            fd, tmp_path = tempfile.mkstemp(dir=self.cache_dir, prefix="universe_cache_", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
                os.replace(tmp_path, self.cache_file)
            except Exception:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
                raise
        except Exception as exc:
            logging.getLogger("MoneyMintterMasterBlueprint").warning(f"[Universe Layer 1] Failed to save persisted cache atomically: {exc}")
