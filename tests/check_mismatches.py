import json

with open("tests/experiment_a_pilot_results.json", "r", encoding="utf-8") as f:
    data = json.load(f)

mismatches = [r for r in data["results"] if not r["status_match"]]
print(f"Total mismatches: {len(mismatches)}")

strata_counts = {}
for r in mismatches:
    s = r["stratum"]
    strata_counts[s] = strata_counts.get(s, 0) + 1
print("Per-stratum counts:", strata_counts)

reasons = {}
for r in mismatches:
    sip_reason = r["sip"]["exclusion_reason"]
    iex_reason = r["iex"]["exclusion_reason"]
    key = f"SIP: '{sip_reason}' vs IEX: '{iex_reason}'"
    reasons[key] = reasons.get(key, 0) + 1
print("Filter-level causes:", reasons)
