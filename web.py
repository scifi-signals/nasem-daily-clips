#!/usr/bin/env python3
"""Web frontend for NASEM Daily Clips Aggregator.

Flask app with tabbed interface: NASEM institutional coverage + PNAS papers.
API key stays server-side.
"""

import os
import sys
import time
import json
import logging
from collections import defaultdict
from datetime import datetime, timezone

from flask import Flask, request, jsonify, render_template_string

from clips import run_pipeline, format_html_nasem, format_html_pnas

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 1_000_000

logger = logging.getLogger("clips-web")

# --- Rate Limiting ---

_rate_limits = defaultdict(list)
RATE_LIMIT_WINDOW = 60
RATE_LIMIT_MAX = 3


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
        .form-group { flex: 1; }
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
            width: 100%;
        }
        button:hover { background: #154360; }
        button:disabled { background: #94a3b8; cursor: not-allowed; }

        /* Tabs */
        .tabs {
            display: flex;
            gap: 0;
            margin-bottom: 0;
            margin-top: 24px;
        }
        .tab {
            padding: 12px 24px;
            background: #e2e8f0;
            border: 1px solid #d0d5dd;
            border-bottom: none;
            border-radius: 8px 8px 0 0;
            font-size: 14px;
            font-weight: 600;
            font-family: inherit;
            cursor: pointer;
            color: #666;
            width: auto;
            transition: all 0.2s;
        }
        .tab:hover { background: #f1f5f9; }
        .tab.active {
            background: white;
            color: #1a5276;
            border-bottom: 1px solid white;
            margin-bottom: -1px;
            z-index: 1;
        }
        .tab .count {
            background: #94a3b8;
            color: white;
            padding: 1px 7px;
            border-radius: 10px;
            font-size: 12px;
            margin-left: 6px;
        }
        .tab.active .count { background: #1a5276; }
        .tab-content {
            display: none;
            background: white;
            border: 1px solid #d0d5dd;
            border-radius: 0 8px 8px 8px;
            padding: 20px;
            min-height: 200px;
        }
        .tab-content.active { display: block; }

        .spinner {
            display: none;
            text-align: center;
            padding: 40px;
            color: #666;
        }
        .spinner.active { display: block; }
        .spinner .dot {
            display: inline-block;
            width: 8px; height: 8px;
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
        .error {
            background: #fef2f2;
            border: 1px solid #fecaca;
            color: #991b1b;
            padding: 12px 16px;
            border-radius: 8px;
            font-size: 14px;
        }
        .stats {
            background: #f8fafc;
            border: 1px solid #e2e8f0;
            border-radius: 8px;
            padding: 12px 16px;
            margin-bottom: 16px;
            font-size: 13px;
            color: #666;
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
        .empty-state {
            text-align: center;
            padding: 40px;
            color: #999;
            font-size: 15px;
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

        <form id="form" onsubmit="return handleSubmit(event)">
            <div class="form-row">
                <div class="form-group">
                    <label for="days">Look Back</label>
                    <select id="days" name="days">
                        <option value="1" selected>1 day</option>
                        <option value="2">2 days</option>
                        <option value="3">3 days</option>
                        <option value="5">5 days</option>
                    </select>
                </div>
            </div>
            <button type="submit" id="submit-btn">Generate Daily Clips</button>
        </form>

        <div class="spinner" id="spinner">
            <div><span class="dot"></span><span class="dot"></span><span class="dot"></span></div>
            <p style="margin-top:12px;">Scanning sources, verifying articles, and categorizing...</p>
            <p style="margin-top:6px;font-size:13px;color:#999;">This takes 1-2 minutes</p>
        </div>

        <div id="error-container"></div>

        <div id="results-container" style="display:none;">
            <div id="stats-container"></div>

            <div class="tabs">
                <button class="tab active" onclick="switchTab('nasem', this)" id="tab-nasem">
                    NASEM Coverage <span class="count" id="nasem-count">0</span>
                </button>
                <button class="tab" onclick="switchTab('pnas', this)" id="tab-pnas">
                    PNAS Papers <span class="count" id="pnas-count">0</span>
                </button>
            </div>

            <div class="tab-content active" id="content-nasem"></div>
            <div class="tab-content" id="content-pnas"></div>

            <div style="margin-top:12px;">
                <button class="copy-btn" onclick="copyTab('nasem')">Copy NASEM HTML</button>
                <button class="copy-btn" onclick="copyTab('pnas')">Copy PNAS HTML</button>
            </div>
        </div>

        <div class="footer">
            NASEM Daily Clips Aggregator &mdash; National Academies of Sciences, Engineering, and Medicine
        </div>
    </div>

    <script>
    function switchTab(tab, btn) {
        document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
        document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
        btn.classList.add('active');
        document.getElementById('content-' + tab).classList.add('active');
    }

    async function handleSubmit(e) {
        e.preventDefault();
        const days = document.getElementById('days').value;
        const spinner = document.getElementById('spinner');
        const btn = document.getElementById('submit-btn');
        const errorContainer = document.getElementById('error-container');
        const resultsContainer = document.getElementById('results-container');

        errorContainer.innerHTML = '';
        resultsContainer.style.display = 'none';
        spinner.classList.add('active');
        btn.disabled = true;

        try {
            const resp = await fetch('/generate', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({days: parseInt(days)})
            });
            const data = await resp.json();

            if (!resp.ok) {
                errorContainer.innerHTML = '<div class="error">' + (data.error || 'Unknown error') + '</div>';
                return;
            }

            // Stats
            const s = data.stats;
            const excluded = s.nasem_articles - (s.nasem_included || 0);
            document.getElementById('stats-container').innerHTML =
                '<div class="stats">' +
                'Scanned <strong>' + s.raw_total + '</strong> raw articles &rarr; ' +
                '<strong>' + (s.nasem_included || 0) + '</strong> relevant NASEM clips in ' +
                '<strong>' + s.nasem_groups + '</strong> groups + ' +
                '<strong>' + s.pnas_articles + '</strong> PNAS articles' +
                (excluded > 0 ? '<br><span style="font-size:12px;color:#999;">' + excluded + ' tangential results excluded by AI</span>' : '') +
                '</div>';

            // Tab counts
            document.getElementById('nasem-count').textContent = s.nasem_included || 0;
            document.getElementById('pnas-count').textContent = s.pnas_articles;

            // NASEM content
            if (data.nasem_html) {
                document.getElementById('content-nasem').innerHTML = data.nasem_html;
            } else {
                document.getElementById('content-nasem').innerHTML =
                    '<div class="empty-state">No NASEM institutional coverage found for this period.</div>';
            }

            // PNAS content
            if (data.pnas_html) {
                document.getElementById('content-pnas').innerHTML = data.pnas_html;
            } else {
                document.getElementById('content-pnas').innerHTML =
                    '<div class="empty-state">No PNAS paper coverage found for this period.</div>';
            }

            // Show results, switch to NASEM tab
            resultsContainer.style.display = 'block';
            document.getElementById('tab-nasem').click();

        } catch (err) {
            errorContainer.innerHTML = '<div class="error">Network error: ' + err.message + '</div>';
        } finally {
            spinner.classList.remove('active');
            btn.disabled = false;
        }
    }

    function copyTab(tab) {
        const content = document.getElementById('content-' + tab);
        // Get the styled card div inside (skip empty states)
        const card = content.querySelector('div[style*="font-family"]');
        const text = card ? card.outerHTML : content.innerHTML;

        navigator.clipboard.writeText(text).then(() => {
            // Find the button that was clicked
            const btns = document.querySelectorAll('.copy-btn');
            const btn = tab === 'nasem' ? btns[0] : btns[1];
            const orig = btn.textContent;
            btn.textContent = 'Copied!';
            btn.classList.add('copied');
            setTimeout(() => {
                btn.textContent = orig;
                btn.classList.remove('copied');
            }, 2000);
        });
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
    if not isinstance(days, int) or days < 1 or days > 7:
        return jsonify({"error": "Days must be between 1 and 7."}), 400

    try:
        result = run_pipeline(days=days, use_claude=True)

        if "error" in result:
            return jsonify({"error": result["error"]}), 404

        nasem_html = ""
        nasem_included = 0
        if result["nasem_articles"]:
            nasem_html = format_html_nasem(
                result["nasem_articles"],
                result["nasem_categories"],
                result["date_label"],
            )
            # Count how many articles Claude actually included
            excluded = set(result["nasem_categories"].get("excluded_indices", []))
            for group in result["nasem_categories"].get("groups", []):
                nasem_included += len(group.get("articles", []))

        pnas_html = ""
        if result["pnas_articles"]:
            pnas_html = format_html_pnas(
                result["pnas_articles"],
                result["date_label"],
            )

        stats = result["stats"]
        stats["nasem_included"] = nasem_included

        return jsonify({
            "nasem_html": nasem_html,
            "pnas_html": pnas_html,
            "stats": stats,
        })

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
