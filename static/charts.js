/*
 * charts.js
 * ---------
 * Live updates via Server-Sent Events + Chart.js renderers (momentum / xG / possession).
 * Only activates on /match/<id> pages tagged with data-phase="live".
 * PRE-MATCH and POST-MATCH pages render nothing here.
 */

(function () {
    "use strict";

    const matchId = document.body.dataset.matchId;
    const phase   = document.body.dataset.phase;
    if (!matchId) return;

    // ---- Color tokens (kept in sync with style.css) ----
    const COLORS = {
        green:  "#00ff88",
        blue:   "#58a6ff",
        red:    "#ff4444",
        yellow: "#f0b429",
        grid:   "#30363d",
        text:   "#8b949e",
    };

    // ---- SSE: stream score / status updates from /stream/<id> ----
    function connectStream() {
        if (typeof EventSource === "undefined") return;
        const es = new EventSource("/stream/" + matchId);
        es.onmessage = (e) => {
            try {
                const data = JSON.parse(e.data || "{}");
                if (data.home_score != null) {
                    document.querySelectorAll("[data-home-score]").forEach(el => {
                        el.textContent = data.home_score;
                    });
                }
                if (data.away_score != null) {
                    document.querySelectorAll("[data-away-score]").forEach(el => {
                        el.textContent = data.away_score;
                    });
                }
                if (data.status) {
                    document.querySelectorAll("[data-status]").forEach(el => {
                        el.textContent = data.status;
                    });
                }
            } catch (err) {
                console.warn("SSE parse error", err);
            }
        };
        es.onerror = () => { /* browser auto-reconnects */ };
    }

    // ---- Chart.js: momentum (line) + possession (doughnut) ----
    function renderMomentumChart() {
        const ctx = document.getElementById("momentumChart");
        if (!ctx || typeof Chart === "undefined") return;
        // Placeholder data: real momentum comes from /stream + DB aggregate
        const minutes = Array.from({ length: 45 }, (_, i) => i + 1);
        new Chart(ctx, {
            type: "line",
            data: {
                labels: minutes,
                datasets: [
                    {
                        label: "홈 모멘텀",
                        data: minutes.map(() => 0),
                        borderColor: COLORS.green,
                        backgroundColor: COLORS.green + "33",
                        tension: 0.3,
                        fill: true,
                    },
                    {
                        label: "원정 모멘텀",
                        data: minutes.map(() => 0),
                        borderColor: COLORS.blue,
                        backgroundColor: COLORS.blue + "33",
                        tension: 0.3,
                        fill: true,
                    },
                ],
            },
            options: {
                plugins: { legend: { labels: { color: COLORS.text } } },
                scales: {
                    x: { grid: { color: COLORS.grid }, ticks: { color: COLORS.text } },
                    y: { grid: { color: COLORS.grid }, ticks: { color: COLORS.text } },
                },
            },
        });
    }

    function renderPossessionChart() {
        const ctx = document.getElementById("possessionChart");
        if (!ctx || typeof Chart === "undefined") return;
        new Chart(ctx, {
            type: "doughnut",
            data: {
                labels: ["홈", "원정"],
                datasets: [{
                    data: [50, 50],
                    backgroundColor: [COLORS.green, COLORS.blue],
                    borderColor: COLORS.grid,
                    borderWidth: 2,
                }],
            },
            options: {
                plugins: { legend: { labels: { color: COLORS.text } } },
                cutout: "65%",
            },
        });
    }

    // ---- Init ----
    if (phase === "live") {
        connectStream();
        renderMomentumChart();
        renderPossessionChart();
    }
})();
