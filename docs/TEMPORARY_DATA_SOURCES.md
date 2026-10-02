# Temporary Market-Data Stack

Money Mintter uses provider-neutral boundaries so a temporary market-data vendor can be replaced by IBKR later without changing the strategy or paper-trading logic.

## Temporary provider

The current development adapter targets Financial Modeling Prep (FMP) Stable APIs.

### Discovery

- `/most-actives` is the required universe endpoint.
- `/biggest-gainers` and `/biggest-losers` are optional enrichments.
- The adapter converts the provider snapshot into normalized `Candidate` records.
- Liquidity is deliberately conservative for mover-only symbols because the deep pipeline must not assume a top mover is liquid enough to trade.

The resulting flow is:

```
FMP market movers
      |
      v
Money Mintter normalized Candidate
      |
      v
OpportunityAnalyzer
      |
      v
CandidateRanker (maximum 5)
```

## Temporary intraday data

The `FmpMinuteData` adapter reads FMP's `/historical-chart/1min` endpoint and converts the response into validated Money Mintter `Candle` objects.

It:

- accepts explicit date windows;
- attaches an explicit exchange timezone to naive provider timestamps;
- sorts candles chronologically;
- rejects duplicate minute timestamps;
- never pushes provider-specific fields into the strategy layer.

This is sufficient for historical intraday research and provider-integration testing.

## Credentials

Do not commit API keys.

Copy the repository `.env.example` values into your local environment or deployment secret store. The code accepts the key through the `FmpClient` constructor.

The current FMP plan determines which endpoints, intraday history, real-time data, and request capacity are available. The temporary integration therefore does not assume that a free account exposes the entire development stack.

## Replacement path

When IBKR is available, implement an IBKR provider behind the existing `Screener`/market-data boundaries and remove FMP from runtime configuration. Strategy, risk, PaperBroker, persistence and recovery should remain unchanged.
