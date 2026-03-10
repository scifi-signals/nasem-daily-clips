# NASEM Daily Clips Aggregator

Automates the daily news clips workflow for NASEM's Office of News and Public Information (ONPI).

## Usage

```bash
python clips.py                    # Today's clips, plain text
python clips.py --days 3           # Last 3 days (for Mondays)
python clips.py --html             # Styled HTML email card
python clips.py --json             # JSON output
python clips.py --no-claude        # Skip AI categorization (scan only)
python web.py                      # Web interface on port 8896
```

## Architecture

Two scripts:
1. `clips.py` — CLI tool and core library. Pipeline: scan Google News RSS → filter → dedup → rank → Claude categorization → format.
2. `web.py` — Flask web frontend for staff review. Calls the same pipeline functions.

### Pipeline Steps
1. **Scan** — Google News RSS for 12 NASEM search terms + 3 PNAS terms
2. **Filter** — Remove NASEM's own pages and press release wires
3. **Deduplicate** — Jaccard title similarity (0.55 threshold)
4. **Rank** — Sort by recency × outlet prominence (tiered scoring)
5. **Categorize** — Claude groups by topic, flags reposts/negatives, picks PNAS clip + "News You Can Use"
6. **Format** — HTML email card, plain text, or JSON

### Search Terms (from ONPI tutorial)
- Organization names: NASEM, NAS, NAM, NAE, TRB, GRP, IOM
- Key people: Marcia McNutt, Victor Dzau, Neil Shubin, Monica Bertagnolli, Tsu-Jae Liu
- PNAS/PNAS Nexus

## Dependencies

- `anthropic` — Claude API for categorization
- `httpx` — HTTP client for RSS feeds
- `beautifulsoup4` + `lxml` — RSS parsing
- `flask` — web frontend

## API Key

Set `ANTHROPIC_API_KEY` environment variable.
Optional: `CLIPS_MODEL` to override model (default: claude-sonnet-4-20250514).

## Server

Port 8896 (web frontend). Rate limited to 3 requests/minute per IP.
