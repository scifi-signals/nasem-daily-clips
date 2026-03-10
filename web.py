#!/usr/bin/env python3
"""Web frontend for NASEM Daily Clips Aggregator.

Flask app for staff to generate, review, and edit daily clips before sending.
API key stays server-side.
"""

import os
import sys
import time
import json
import logging
from collections import defaultdict
from datetime import datetime, timezone
from functools import wraps

from flask import Flask, request, jsonify, render_template_string

from clips import (
    scan_all_sources, filter_articles, deduplicate, rank_articles,
    categorize_with_claude, format_html, format_plain, format_json,
    run_pipeline,
)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 1_000_000

logger = logging.getLogger("clips-web")

# --- Rate Limiting ---

_rate_limits = defaultdict(list)
RATE_LIMIT_WINDOW = 60
RATE_LIMIT_MAX = 3  # Clips generation is expensive, limit more


def _check_rate_limit(ip: str) -> bool:
    now = time.time()
    _rate_limits[ip] = [t for t in _rate_limits[ip] if now - t < RATE_LIMIT_WINDOW]
    if len(_rate_limits[ip]) >= RATE_LIMIT_MAX:
        return False
    _rate_limits[ip].append(now)
    return True


# --- HTML Template ---

PAGE_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>NASEM Daily Clips</title>
    <link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: 'DM Sans', -apple-system, BlinkMacSystemFont, sans-serif;
            background: #f5f7fa;
            color: #333;
            min-height: 100vh;
        }
        .container {
            max-width: 800px;
            margin: 0 auto;
            padding: 40px 20px;
        }
        h1 {
            font-size: 28px;
            font-weight: 700;
            color: #1a5276;
            margin-bottom: 8px;
        }
        .subtitle {
            color: #666;
            font-size: 15px;
            margin-bottom: 32px;
        }
        .form-row {
            display: flex;
            gap: 16px;
            margin-bottom: 20px;
            align-items: end;
        }
        .form-group {
            flex: 1;
        }
        label {
            display: block;
            font-weight: 600;
            margin-bottom: 6px;
            font-size: 14px;
            color: #444;
        }
        select {
            width: 100%;
            padding: 12px 14px;
            border: 1px solid #d0d5dd;
            border-radius: 8px;
            font-size: 15px;
            font-family: inherit;
            background: white;
            transition: border-color 0.2s;
        }
        select:focus {
            outline: none;
            border-color: #1a5276;
            box-shadow: 0 0 0 3px rgba(26,82,118,0.1);
        }
        button {
            background: #1a5276;
            color: white;
            border: none;
            padding: 12px 28px;
            border-radius: 8px;
            font-size: 15px;
            font-weight: 600;
            font-family: inherit;
            cursor: pointer;
            transition: background 0.2s;
            width: 100%;
        }
        button:hover { background: #154360; }
        button:disabled {
            background: #94a3b8;
            cursor: not-allowed;
        }
        .info-box {
            background: #eff6ff;
            border: 1px solid #bfdbfe;
            border-radius: 8px;
            padding: 12px 16px;
            font-size: 13px;
            color: #1e40af;
            margin-bottom: 20px;
            line-height: 1.5;
        }
        .spinner {
            display: none;
            text-align: center;
            padding: 40px;
            color: #666;
        }
        .spinner.active { display: block; }
        .spinner .dot {
            display: inline-block;
            width: 8px;
            height: 8px;
            border-radius: 50%;
            background: #1a5276;
            margin: 0 4px;
            animation: bounce 1.4s ease-in-out infinite;
        }
        .spinner .dot:nth-child(2) { animation-delay: 0.16s; }
        .spinner .dot:nth-child(3) { animation-delay: 0.32s; }
        @keyframes bounce {
            0%, 80%, 100% { transform: scale(0); }
            40% { transform: scale(1); }
        }
        #result { margin-top: 24px; }
        .error {
            background: #fef2f2;
            border: 1px solid #fecaca;
            color: #991b1b;
            padding: 12px 16px;
            border-radius: 8px;
            font-size: 14px;
        }
        .copy-btn {
            background: #e2e8f0;
            color: #475569;
            padding: 8px 16px;
            border-radius: 6px;
            font-size: 13px;
            font-weight: 600;
            cursor: pointer;
            border: none;
            margin-top: 12px;
            margin-right: 8px;
            width: auto;
            display: inline-block;
        }
        .copy-btn:hover { background: #cbd5e1; }
        .copy-btn.copied { background: #bbf7d0; color: #166534; }
        .stats {
            background: white;
            border: 1px solid #e0e0e0;
            border-radius: 8px;
            padding: 12px 16px;
            margin-bottom: 16px;
            font-size: 13px;
            color: #666;
        }
        .footer {
            text-align: center;
            margin-top: 48px;
            font-size: 12px;
            color: #999;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>NASEM Daily Clips</h1>
        <p class="subtitle">Automated news coverage digest for the Daily Huddle</p>

        <div class="info-box">
            Scans Google News for all NASEM search terms, deduplicates, and uses AI to categorize by topic,
            flag press release reposts, identify PNAS clips, and pick a "News You Can Use" item.
            Use <strong>3 days</strong> on Mondays to cover the weekend.
        </div>

        <form id="form" onsubmit="return handleSubmit(event)">
            <div class="form-row">
                <div class="form-group">
                    <label for="days">Look Back</label>
                    <select id="days" name="days">
                        <option value="1" selected>1 day (normal)</option>
                        <option value="2">2 days</option>
                        <option value="3">3 days (Monday)</option>
                        <option value="5">5 days</option>
                    </select>
                </div>
                <div class="form-group">
                    <label for="format">Output</label>
                    <select id="format" name="format">
                        <option value="html" selected>Email Card</option>
                        <option value="text">Plain Text</option>
                        <option value="json">JSON</option>
                    </select>
                </div>
            </div>
            <button type="submit" id="submit-btn">Generate Daily Clips</button>
        </form>

        <div class="spinner" id="spinner">
            <div><span class="dot"></span><span class="dot"></span><span class="dot"></span></div>
            <p style="margin-top:12px;">Scanning Google News and categorizing with AI...</p>
            <p style="margin-top:6px;font-size:13px;color:#999;">This takes 30-60 seconds</p>
        </div>

        <div id="result"></div>

        <div class="footer">
            NASEM Daily Clips Aggregator &mdash; National Academies of Sciences, Engineering, and Medicine
        </div>
    </div>

    <script>
    async function handleSubmit(e) {
        e.preventDefault();
        const days = document.getElementById('days').value;
        const format = document.getElementById('format').value;
        const resultDiv = document.getElementById('result');
        const spinner = document.getElementById('spinner');
        const btn = document.getElementById('submit-btn');

        resultDiv.innerHTML = '';
        spinner.classList.add('active');
        btn.disabled = true;

        try {
            const resp = await fetch('/generate', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({days: parseInt(days), format})
            });
            const data = await resp.json();

            if (!resp.ok) {
                resultDiv.innerHTML = '<div class="error">' + (data.error || 'Unknown error') + '</div>';
                return;
            }

            let statsHtml = '';
            if (data.stats) {
                statsHtml = '<div class="stats">' +
                    '<strong>' + data.stats.unique_articles + '</strong> unique articles from ' +
                    '<strong>' + data.stats.raw_articles + '</strong> raw results | ' +
                    '<strong>' + data.stats.groups + '</strong> topic groups' +
                    '</div>';
            }

            if (format === 'html') {
                resultDiv.innerHTML = statsHtml + data.result +
                    '<button class="copy-btn" onclick="copyResult(this, \'html\')">Copy HTML</button>' +
                    '<button class="copy-btn" onclick="copyResult(this, \'text\')">Copy Text</button>';
            } else if (format === 'json') {
                resultDiv.innerHTML = statsHtml +
                    '<pre style="background:#1e293b;color:#e2e8f0;padding:16px;border-radius:8px;overflow-x:auto;font-size:13px;line-height:1.5;">' +
                    escapeHtml(data.result) + '</pre>' +
                    '<button class="copy-btn" onclick="copyResult(this, \'text\')">Copy JSON</button>';
            } else {
                resultDiv.innerHTML = statsHtml +
                    '<pre style="background:white;padding:16px;border-radius:8px;border:1px solid #e0e0e0;font-size:14px;line-height:1.6;white-space:pre-wrap;">' +
                    escapeHtml(data.result) + '</pre>' +
                    '<button class="copy-btn" onclick="copyResult(this, \'text\')">Copy Text</button>';
            }
        } catch (err) {
            resultDiv.innerHTML = '<div class="error">Network error: ' + err.message + '</div>';
        } finally {
            spinner.classList.remove('active');
            btn.disabled = false;
        }
    }

    function copyResult(btn, mode) {
        const resultDiv = document.getElementById('result');
        let text;
        if (mode === 'html') {
            // Find the clips card div (skip the stats div)
            const cards = resultDiv.querySelectorAll(':scope > div[style]');
            const card = cards.length > 1 ? cards[cards.length - 1] : cards[0];
            if (card && !card.classList.contains('stats')) {
                text = card.outerHTML;
            }
        }
        if (!text) {
            const pre = resultDiv.querySelector('pre');
            text = pre ? pre.textContent : resultDiv.textContent;
        }
        navigator.clipboard.writeText(text).then(() => {
            const orig = btn.textContent;
            btn.textContent = 'Copied!';
            btn.classList.add('copied');
            setTimeout(() => {
                btn.textContent = orig;
                btn.classList.remove('copied');
            }, 2000);
        });
    }

    function escapeHtml(str) {
        return str.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    }
    </script>
</body>
</html>"""


@app.route("/")
def index():
    return render_template_string(PAGE_TEMPLATE)


@app.route("/generate", methods=["POST"])
def generate():
    ip = request.remote_addr
    if not _check_rate_limit(ip):
        return jsonify({"error": "Rate limit exceeded. Max 3 requests per minute."}), 429

    data = request.get_json()
    if not data:
        return jsonify({"error": "Invalid request."}), 400

    days = data.get("days", 1)
    fmt = data.get("format", "html")

    if not isinstance(days, int) or days < 1 or days > 7:
        return jsonify({"error": "Days must be between 1 and 7."}), 400

    if fmt not in ("html", "text", "json"):
        return jsonify({"error": "Format must be html, text, or json."}), 400

    try:
        # Run pipeline with stats tracking
        from datetime import timedelta
        from clips import (
            scan_all_sources, filter_articles, deduplicate, rank_articles,
            categorize_with_claude, format_html as fmt_html,
            format_plain as fmt_plain, format_json as fmt_json,
        )

        today = datetime.now(timezone.utc)
        if days == 1:
            date_label = today.strftime("%A, %B %d, %Y")
        else:
            start = (today - timedelta(days=days)).strftime("%B %d")
            end = today.strftime("%B %d, %Y")
            date_label = f"{start} — {end}"

        raw = scan_all_sources(days)
        if not raw:
            return jsonify({"error": "No articles found. Google News may be temporarily unavailable."}), 404

        filtered = filter_articles(raw)
        unique = deduplicate(filtered)
        if not unique:
            return jsonify({"error": "No articles found after filtering duplicates and press wires."}), 404

        ranked = rank_articles(unique)
        if len(ranked) > 50:
            ranked = ranked[:50]

        categories = categorize_with_claude(ranked)

        stats = {
            "raw_articles": len(raw),
            "unique_articles": len(ranked),
            "groups": len(categories.get("groups", [])),
        }

        if fmt == "html":
            result = fmt_html(ranked, categories, date_label)
        elif fmt == "json":
            result = fmt_json(ranked, categories, date_label)
        else:
            result = fmt_plain(ranked, categories, date_label)

        return jsonify({"result": result, "stats": stats})

    except Exception as e:
        logger.error(f"Pipeline error: {e}", exc_info=True)
        return jsonify({"error": f"Pipeline failed: {e}"}), 500


@app.route("/health")
def health():
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    return jsonify({"status": "ok", "api_key_configured": has_key})


if __name__ == "__main__":
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY environment variable not set.", file=sys.stderr)
        sys.exit(1)

    port = int(os.environ.get("PORT", 8896))
    print(f"Starting NASEM Daily Clips on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
