/**
 * The three view-models, exactly as FastAPI emits them.
 *
 * The contract (which field means what, and that `pending` and `failed` are
 * different verdicts) is pytest's job. This suite answers the other half: given
 * that JSON, does the screen read. So nothing here needs Python, a database or a
 * snapshot.
 */

const currency = "CNY";

/** Today's panel as `/ui/history.json` sends it (the widget sends the same, minus `start_of_day`). */
const today = {
  spent_today: 12.4,
  spent_yesterday: 18.1,
  normal_spend: 16.0,
  projected_spend: 27.4,
  current_balance: 87.6,
  prev_balance: 100.0,
  start_of_day: "2026-11-14T00:00:00+00:00",
  current_ts: "2026-11-14T15:30:00+00:00",
};

const summary = {
  window_hours: 24,
  slice_minutes: 5,
  total_slices: 288,
  intervals_with_spend: 40,
  usage_minutes: 200,
  enough_data: true,
  min_intervals_for_baseline: 10,
  baseline_source: "history",
  median_interval_spend: 0.19,
  avg_interval_spend: 0.31,
  spike_threshold: 0.62,
  below_floor: 0.08,
  unusually_high_count: 6,
  normal_count: 28,
  below_count: 6,
};

export const pacing = { label: "above normal", tone: "warn" };

export const widget = { available: true, currency, pacing, today, summary };

export const widgetEmpty = {
  available: false,
  reason: "no successful snapshot yet",
};

/**
 * Fourteen complete days. Two are deliberately awkward: 11-04 holds no
 * snapshots at all (`spend: 0`, so its bar is absent and its slot is kept), and
 * 11-09 has no usage, so its cost-per-hour is null.
 */
const days = [
  ["2026-10-31", 18.4, 180],
  ["2026-11-01", 24.1, 240],
  ["2026-11-02", 9.7, 95],
  ["2026-11-03", 31.2, 300],
  ["2026-11-04", 0, 0],
  ["2026-11-05", 17.9, 175],
  ["2026-11-06", 22.6, 230],
  ["2026-11-07", 12.3, 120],
  ["2026-11-08", 28.8, 280],
  ["2026-11-09", 20.5, 0],
  ["2026-11-10", 26.7, 265],
  ["2026-11-11", 15.1, 150],
  ["2026-11-12", 19.8, 195],
  ["2026-11-13", 18.1, 180],
];

export const series = days.map(([date, spend, usage_minutes]) => ({
  date,
  ts: `${date}T00:00:00+00:00`,
  spend,
  usage_minutes,
  intervals_with_spend: Math.round(usage_minutes / 5),
  cost_per_minute: usage_minutes > 0 ? spend / usage_minutes : null,
}));

export function history({ days: range = 14, available = 14 } = {}) {
  const shown = range > 0 ? series.slice(-range) : series;
  return {
    available: true,
    currency,
    now: "2026-11-14T15:30:00+00:00",
    slice_minutes: 5,
    today,
    pacing,
    summary,
    series: shown,
    available_days: available,
    days: range,
  };
}

/**
 * The analysis table: one row per verdict, because the point of the block is
 * that `investigate`, `actionable`, `fine`, `failed` and `analysing` read
 * differently at a glance.
 */
export const analysis = {
  configured: true,
  status: "all",
  max_attempts: 3,
  has_more: true,
  next_before: "2026-11-13T09:00:00+00:00",
  rows: [
    {
      start_local: "2026-11-14T14:25:00+00:00",
      spend: 4.82,
      requests: 447,
      cache_pct: 94,
      explained_pct: 92,
      label: "high activity, well cached",
      summary: "447 model-284 spans explain the drop.",
      status: { label: "fine", tone: "ok" },
      attempts: 1,
    },
    {
      start_local: "2026-11-14T11:05:00+00:00",
      spend: 2.15,
      requests: 128,
      cache_pct: 41,
      explained_pct: 190,
      label: "over-attributed",
      summary:
        "28 model-56 spans carry more traced cost than the balance lost.",
      status: { label: "investigate", tone: "bad" },
      attempts: 2,
    },
    {
      start_local: "2026-11-14T08:40:00+00:00",
      spend: 1.02,
      requests: 64,
      cache_pct: 12,
      explained_pct: 88,
      label: "cache cold",
      summary: "A new prompt prefix disabled the cache for the window.",
      status: { label: "actionable", tone: "warn" },
      attempts: 1,
    },
    {
      start_local: "2026-11-13T09:00:00+00:00",
      spend: 0.88,
      requests: null,
      cache_pct: null,
      explained_pct: null,
      label: "analysis failed",
      summary: "Phoenix could not be queried -- giving up: connect timeout",
      status: { label: "failed", tone: "bad" },
      attempts: 3,
    },
    {
      start_local: "2026-11-13T07:55:00+00:00",
      spend: 0.71,
      requests: null,
      cache_pct: null,
      explained_pct: null,
      label: "analysis pending",
      summary: "Awaiting the next analysis pass.",
      status: { label: "analysing", tone: "neutral" },
      attempts: 0,
    },
  ],
};

export const analysisUnconfigured = {
  configured: false,
  status: "all",
  max_attempts: null,
  rows: [],
  has_more: false,
  next_before: null,
};
