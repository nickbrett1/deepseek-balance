<script>
  /**
   * The drill-in: today, then the three daily series, then why recent intervals
   * were high.
   *
   * The widget links here because a dashboard card cannot hold a chart. What
   * this page adds over the card is *comparison*: today against the typical day,
   * today against the last fortnight, and each unusually-high interval against
   * the diagnosis of what caused it.
   */
  import { onMount } from "svelte";
  import AppShell from "$lib/components/nb/AppShell.svelte";
  import StatStrip from "$lib/components/nb/StatStrip.svelte";
  import BarSeries from "$lib/components/nb/BarSeries.svelte";
  import DataTable from "$lib/components/nb/DataTable.svelte";
  import { getJson, historyPath, localIso } from "$lib/api.js";
  import {
    axisDuration,
    axisNumber,
    dayLabel,
    duration,
    integer,
    minorInt,
    minorUnit,
    money,
    moneyShort,
    percent,
    updatedLine,
    whenCell,
  } from "$lib/format.js";

  const RANGES = [
    { value: "14", label: "14 days" },
    { value: "30", label: "Last month" },
    { value: "0", label: "All time" },
  ];

  const STATUSES = [
    { value: "all", label: "All" },
    { value: "investigate", label: "Investigate" },
    { value: "actionable", label: "Actionable" },
    { value: "fine", label: "Fine" },
    { value: "pending", label: "Analysing" },
  ];

  let days = $state(14);
  let data = $state(null);
  let historyError = $state("");

  let status = $state("all");
  let analysis = $state(null);
  let analysisError = $state("");
  let before = $state(null);
  let loadingAnalysis = $state(false);

  async function loadHistory(controller) {
    try {
      data = await getJson(historyPath({ days, now: localIso() }), {
        signal: controller?.signal,
      });
      historyError = "";
    } catch (failure) {
      if (failure.name !== "AbortError") historyError = failure.message;
    }
  }

  async function loadAnalysis() {
    loadingAnalysis = true;
    try {
      const query = new URLSearchParams({ limit: "15", status });
      if (before) query.set("before", before);
      analysis = await getJson(`/ui/analysis.json?${query}`);
      analysisError = "";
    } catch (failure) {
      analysisError = failure.message;
    } finally {
      loadingAnalysis = false;
    }
  }

  onMount(() => {
    const controller = new AbortController();
    loadHistory(controller);
    loadAnalysis();
    return () => controller.abort();
  });

  function chooseRange(value) {
    if (value === String(days)) return;
    days = Number(value);
    loadHistory();
  }

  function chooseStatus(value) {
    if (value === status) return;
    status = value;
    before = null; // paging restarts for a new status
    loadAnalysis();
  }

  function older() {
    if (!analysis?.next_before) return;
    before = analysis.next_before;
    loadAnalysis();
  }

  const currency = $derived(data?.currency ?? "");
  const today = $derived(data?.today ?? {});
  const availableDays = $derived(data?.available_days ?? 0);

  /** The day's six numbers, as the stat strip's cards. */
  const stats = $derived.by(() => {
    if (!data) return [];
    return [
      {
        label: "Spent today",
        value: money(today.spent_today, currency),
        sub: data.pacing?.label ?? "",
        tone: data.pacing?.tone ?? "",
      },
      {
        label: "Spend yesterday",
        value: money(today.spent_yesterday, currency),
        sub: "last complete day",
      },
      {
        label: "Typical day",
        value: money(today.normal_spend, currency),
        sub: "median, last 14 days",
      },
      {
        label: "Projected today",
        value: money(today.projected_spend, currency),
        sub: "at the current rate",
      },
      {
        label: "Balance now",
        value: money(today.current_balance, currency),
        sub: updatedLine(today.current_ts),
      },
      {
        label: "Started the day at",
        value: money(today.prev_balance, currency),
        sub: "before any spend",
      },
    ];
  });

  /** Today's interval breakdown — the same rows the widget shows, plus the bands. */
  const summary = $derived(data?.summary ?? {});
  const summaryRows = $derived.by(() => {
    const s = summary;
    const total = s.intervals_with_spend || 0;
    const rows = [
      { id: "intervals", label: "Intervals with spend", value: integer(total) },
      { id: "usage", label: "Usage time", value: duration(s.usage_minutes) },
      {
        id: "median",
        label: "Median interval",
        value: money(s.median_interval_spend, currency),
      },
    ];
    if (total === 0 || !s.enough_data) return rows;
    return rows.concat([
      {
        id: "high",
        label: "Unusually high",
        value: integer(s.unusually_high_count || 0),
        note: `${percent(s.unusually_high_count, total)} · over ${money(s.spike_threshold, currency)}`,
        tone: "warn",
      },
      {
        id: "normal",
        label: "Around normal",
        value: integer(s.normal_count || 0),
        note: `${percent(s.normal_count, total)} · ${money(s.below_floor, currency)}–${money(s.spike_threshold, currency)}`,
        tone: "info",
      },
      {
        id: "below",
        label: "Below normal",
        value: integer(s.below_count || 0),
        note: `${percent(s.below_count, total)} · under ${money(s.below_floor, currency)}`,
        tone: "ok",
      },
    ]);
  });

  const summaryNote = $derived.by(() => {
    if (!data) return "";
    if ((summary.intervals_with_spend || 0) === 0) return "No spend today.";
    if (summary.enough_data) return "";
    const need = Math.max(
      0,
      (summary.min_intervals_for_baseline || 0) -
        (summary.intervals_with_spend || 0),
    );
    return (
      `Need ${summary.min_intervals_for_baseline} spent intervals to judge unusual spend — ` +
      `${summary.intervals_with_spend} so far${need ? ` (${need} more)` : ""}.`
    );
  });

  /**
   * The three daily series, each with today appended as a projection.
   *
   * A projection is drawn hollow by `bar-series`: today is only part-spent, so
   * its bar is an estimate and must not sit in the row of measurements as if it
   * were one. `null` values keep their slot — a day with no snapshots is a fact.
   */
  const charts = $derived.by(() => {
    if (!data) return [];
    const history = data.series ?? [];
    const todayBar = data.available
      ? {
          label: dayLabel(today.start_of_day),
          projected: true,
        }
      : null;

    const spend = history.map((day) => ({
      label: dayLabel(day.date),
      value: day.spend,
      display: moneyShort(day.spend, currency),
    }));
    if (todayBar) {
      const value = today.projected_spend ?? today.spent_today ?? 0;
      spend.push({ ...todayBar, value, display: moneyShort(value, currency) });
    }

    const usage = history.map((day) => ({
      label: dayLabel(day.date),
      value: day.usage_minutes,
      display: duration(day.usage_minutes),
    }));
    if (todayBar) {
      const value = summary.usage_minutes ?? 0;
      usage.push({ ...todayBar, value, display: duration(value) });
    }

    // Cost per active hour in minor units (fen / cents): "cheaper use" reads as
    // a smaller number, and the unit is the one the reader's currency uses.
    const scale = 100;
    const minor = minorUnit(currency);
    const cost = history.map((day) => {
      const value =
        day.usage_minutes > 0
          ? ((day.spend * 60) / day.usage_minutes) * scale
          : null;
      return {
        label: dayLabel(day.date),
        value,
        display: value == null ? "—" : `${minorInt(value)} ${minor}/h`,
      };
    });
    if (todayBar && (summary.usage_minutes ?? 0) > 0) {
      const value = ((today.spent_today * 60) / summary.usage_minutes) * scale;
      cost.push({
        ...todayBar,
        value,
        display: `${minorInt(value)} ${minor}/h`,
      });
    }

    return [
      {
        key: "spend",
        title: "Spend per day",
        unit: currency || "currency",
        note: "Today's bar is dashed = projected full-day spend. Hover for detail.",
        bars: spend,
        axisFormat: axisNumber,
      },
      {
        key: "usage",
        title: "Usage time per day",
        unit: "minutes with spend × slices",
        note: "A rough proxy for active time, not wall-clock open.",
        bars: usage,
        axisFormat: axisDuration,
      },
      {
        key: "cost",
        title: "Cost per hour",
        unit: `${minor}/hour`,
        note: "Spend ÷ active hours. Lower = cheaper use; blank days had no usage.",
        bars: cost,
        axisFormat: minorInt,
      },
    ];
  });

  /** Averages over *complete* days only: today's projection must not move them. */
  const averages = $derived.by(() => {
    const history = (data?.series ?? []).filter(
      (day) => day.spend > 0 || day.usage_minutes > 0,
    );
    if (!history.length) return null;
    const count = history.length;
    const spend = history.reduce((sum, day) => sum + day.spend, 0);
    const usage = history.reduce((sum, day) => sum + day.usage_minutes, 0);
    return {
      label:
        days === 0
          ? `all time since ${history[0].date}`
          : `last ${count} complete day${count === 1 ? "" : "s"}`,
      spend: spend / count,
      usage: usage / count,
      cost: usage > 0 ? ((spend * 60) / usage) * 100 : null,
    };
  });

  /** The range picker's counts: how many complete days each range actually holds. */
  function rangeCount(value) {
    const wanted = Number(value);
    if (!wanted) return availableDays;
    return Math.min(wanted, availableDays);
  }

  const analysisRows = $derived(analysis?.rows ?? []);
  const analysisEmpty = $derived.by(() => {
    if (!analysis) return "Loading…";
    if (!analysis.configured)
      return "Analysis is disabled (no PHOENIX_BASE_URL).";
    if (status !== "all") {
      const label =
        STATUSES.find((entry) => entry.value === status)?.label ?? status;
      return `No unusually-high intervals with status “${label}” yet.`;
    }
    return "No unusually-high intervals analysed yet.";
  });

  const analysisColumns = [
    { label: "interval", width: "minmax(7rem, 1fr)" },
    { label: "spend", align: "end" },
    { label: "req", align: "end", width: "4.5rem" },
    { label: "cache", align: "end", width: "4.5rem" },
    { label: "explains", align: "end", width: "5rem" },
    { label: "diagnosis", width: "minmax(11rem, 2fr)" },
    { label: "status", width: "7rem" },
  ];
</script>

<AppShell
  title="DeepSeek Balance"
  subtitle="spend, usage and why"
  backHref="/"
  backLabel="← widget"
>
  {#if historyError}
    <p class="error">{historyError}</p>
  {:else if !data}
    <p class="hint">Loading…</p>
  {:else}
    <h2 class="section-title">Today’s heartbeat</h2>
    <StatStrip {stats} />

    <h2 class="section-title">
      Spend — today <span class="hint slice"
        >({summary.slice_minutes} min intervals)</span
      >
    </h2>
    <DataTable
      columns={[
        { label: "band" },
        { label: "", align: "end", width: "minmax(9rem, auto)" },
      ]}
      items={summaryRows}
      emptyMessage="No spend today."
    >
      {#snippet row(item)}
        <span>{item.label}</span>
        <span class="num {item.tone ? `tone-${item.tone}` : ''}">
          {item.value}{#if item.note}<span class="hint">
              · {item.note}</span
            >{/if}
        </span>
      {/snippet}
    </DataTable>
    {#if summaryNote}
      <p class="hint note">{summaryNote}</p>
    {/if}

    <div class="section-head">
      <h2 class="section-title">History by day</h2>
      <div class="seg" role="group" aria-label="History range">
        {#each RANGES as range (range.value)}
          <button
            type="button"
            class:on={String(days) === range.value}
            aria-pressed={String(days) === range.value}
            onclick={() => chooseRange(range.value)}
          >
            {range.label}
            <span class="hint count">{rangeCount(range.value)}</span>
          </button>
        {/each}
      </div>
    </div>

    {#if averages}
      <p class="avgline hint">
        <b>{averages.label}</b> · avg spend/day
        <b>{moneyShort(averages.spend, currency)}</b>
        · avg usage/day <b>{duration(averages.usage)}</b>
        · avg cost {averages.cost == null
          ? "—"
          : `${minorInt(averages.cost)} ${minorUnit(currency)}/h`}
      </p>
    {:else}
      <p class="avgline hint">
        No complete days of history yet — the bar is today’s projection.
      </p>
    {/if}

    {#each charts as chart (chart.key)}
      <div class="card chart-card">
        <BarSeries
          title={chart.title}
          unit={chart.unit}
          note={chart.note}
          bars={chart.bars}
          axisFormat={chart.axisFormat}
        />
      </div>
    {/each}

    <div class="section-head">
      <h2 class="section-title">Why were recent intervals unusually high?</h2>
      <div class="seg" role="group" aria-label="Diagnosis status">
        {#each STATUSES as entry (entry.value)}
          <button
            type="button"
            class:on={status === entry.value}
            aria-pressed={status === entry.value}
            onclick={() => chooseStatus(entry.value)}
          >
            {entry.label}
          </button>
        {/each}
      </div>
    </div>

    {#if analysisError}
      <p class="error">Analysis failed to load: {analysisError}</p>
    {:else}
      <DataTable
        columns={analysisColumns}
        items={analysisRows}
        emptyMessage={analysisEmpty}
      >
        {#snippet row(item)}
          <span class="when">{whenCell(item.start_local)}</span>
          <span class="num">{money(item.spend, currency)}</span>
          <span class="num">{integer(item.requests)}</span>
          <span class="num"
            >{item.cache_pct == null ? "—" : `${item.cache_pct}%`}</span
          >
          <span class="num" title="traced cost ÷ balance drop">
            {item.explained_pct == null ? "—" : `${item.explained_pct}%`}
          </span>
          <span class="wrap diag">
            <b>{item.label}</b>
            <div class="hint">{item.summary}</div>
          </span>
          <span
            ><span class="pill {item.status.tone}">{item.status.label}</span
            ></span
          >
        {/snippet}
      </DataTable>

      <div class="pager">
        <button
          type="button"
          class="btn"
          disabled={!analysis?.has_more || loadingAnalysis}
          onclick={older}
        >
          ← Older
        </button>
        <span class="hint">
          {#if analysis?.has_more}
            Showing the most recent intervals. Newest first.
          {:else if analysisRows.length}
            That is every recorded interval.
          {/if}
        </span>
      </div>
    {/if}
  {/if}
</AppShell>

<style>
  .slice {
    font-size: 11px;
    text-transform: none;
    letter-spacing: 0;
  }

  .section-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    flex-wrap: wrap;
    margin-top: 8px;
  }

  .section-head .section-title {
    margin-bottom: 0;
  }

  .seg button .count {
    margin-left: 4px;
    font-variant-numeric: tabular-nums;
  }

  .avgline {
    margin: 10px 0 12px;
  }

  .avgline b {
    color: var(--ink-dim);
    font-variant-numeric: tabular-nums;
  }

  .chart-card {
    padding: 12px 14px 4px;
    margin-bottom: 12px;
  }

  .note {
    margin: -6px 0 12px;
    font-size: 11px;
  }

  .error {
    color: var(--bad);
  }

  .pager {
    display: flex;
    align-items: center;
    gap: 10px;
    margin: 10px 0 24px;
  }

  /* The diagnosis cell is the one cell in the table that is prose, so it opts
     out of the block's one-line truncation — including its inner <div>, which
     the block's global rule would otherwise clamp. */
  .diag :global(div) {
    white-space: normal;
    overflow: visible;
  }
</style>
