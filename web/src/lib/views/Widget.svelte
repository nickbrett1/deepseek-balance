<script>
  /**
   * The homepage widget: the daily heartbeat, in a dashboard card.
   *
   * This screen is *not* an app page. Homepage iframes `/` into a column of
   * dashboard cards, so its job is to be short and to tell the parent how tall
   * it is -- and that resize post is the one contract this file must not break.
   * The rest of the card is two tables: the day's four numbers, and today's
   * interval summary. Both use `data-table` because a widget's columns are two:
   * a label and a right-aligned figure.
   */
  import { onMount } from "svelte";
  import AppShell from "$lib/components/nb/AppShell.svelte";
  import DataTable from "$lib/components/nb/DataTable.svelte";
  import { getJson, widgetPath } from "$lib/api.js";
  import {
    money,
    duration,
    integer,
    percent,
    updatedLine,
  } from "$lib/format.js";

  let data = $state(null);
  let error = $state("");

  onMount(() => {
    const controller = new AbortController();
    getJson(widgetPath(), { signal: controller.signal })
      .then((body) => (data = body))
      .catch((failure) => {
        if (failure.name !== "AbortError") error = failure.message;
      });
    return () => controller.abort();
  });

  /**
   * Tell Homepage how tall we are.
   *
   * Measure `<body>`, never `documentElement`: once the parent stretches this
   * iframe taller than the content, `scrollHeight` starts reporting the iframe's
   * own height and the card grows a little on every tick. `<body>` has no
   * height, so its offsetHeight is the content.
   */
  onMount(() => {
    if (window.parent === window) return;
    const report = () => {
      const height = Math.ceil(document.body ? document.body.offsetHeight : 0);
      if (height > 0) {
        window.parent.postMessage(
          { type: "homepage-iframe-resize", height },
          "*",
        );
      }
    };
    if (document.readyState === "complete") report();
    else window.addEventListener("load", report);
    // The data arrives after load, so one report would always be too short.
    const timer = setInterval(report, 1500);
    return () => {
      clearInterval(timer);
      window.removeEventListener("load", report);
    };
  });

  const currency = $derived(data?.currency ?? "");
  const today = $derived(data?.today ?? {});
  const pacing = $derived(data?.pacing ?? { label: "", tone: "neutral" });

  const heartbeat = $derived([
    {
      id: "spent",
      label: "Spent today",
      value: money(today.spent_today, currency),
      tone: pacing.tone,
      pacing: pacing.label,
    },
    {
      id: "yesterday",
      label: "Spend yesterday",
      value: money(today.spent_yesterday, currency),
    },
    {
      id: "typical",
      label: "Typical day",
      value: money(today.normal_spend, currency),
    },
    {
      id: "projected",
      label: "Projected spend today",
      value: money(today.projected_spend, currency),
    },
  ]);

  const summary = $derived(data?.summary ?? {});

  /**
   * Today's interval breakdown, as rows. The bands are the point: "3 unusually
   * high" is only meaningful next to the threshold that called them high, so
   * each row carries its own threshold.
   */
  const bands = $derived.by(() => {
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
        value: `${integer(s.unusually_high_count || 0)} (${percent(s.unusually_high_count, total)})`,
        note: `over ${money(s.spike_threshold, currency)}`,
        tone: "warn",
      },
      {
        id: "normal",
        label: "Around normal",
        value: `${integer(s.normal_count || 0)} (${percent(s.normal_count, total)})`,
        note: `${money(s.below_floor, currency)}–${money(s.spike_threshold, currency)}`,
        tone: "info",
      },
      {
        id: "below",
        label: "Below normal",
        value: `${integer(s.below_count || 0)} (${percent(s.below_count, total)})`,
        note: `under ${money(s.below_floor, currency)}`,
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

  const columns = [
    { label: "today", width: "1fr" },
    { label: "", align: "end", width: "minmax(6.5rem, auto)" },
  ];
</script>

<AppShell
  title="DeepSeek Balance"
  subtitle={data?.available === false
    ? "no snapshot yet"
    : updatedLine(today.current_ts)}
  status={pacing.label}
  statusTone={pacing.tone}
>
  {#if error}
    <p class="error">{error}</p>
  {:else if !data}
    <p class="hint">Loading…</p>
  {:else if !data.available}
    <p class="hint">Waiting for the first snapshot…</p>
  {:else}
    <div class="balance">
      <div class="figure num">{money(today.current_balance, "")}</div>
      <div class="hint">
        Balance now · {currency} · started the day at {money(
          today.prev_balance,
          currency,
        )}
      </div>
    </div>

    <DataTable {columns} items={heartbeat} emptyMessage="No data yet.">
      {#snippet row(item)}
        <span>{item.label}</span>
        <span class="num {item.tone ? `tone-${item.tone}` : ''}">
          {item.value}
          {#if item.pacing}
            <span class="pill {item.tone}">{item.pacing}</span>
          {/if}
        </span>
      {/snippet}
    </DataTable>

    <DataTable {columns} items={bands} emptyMessage="No spend today." title="">
      {#snippet row(item)}
        <span
          >{item.label}{#if item.note}<span class="hint note">
              · {item.note}</span
            >{/if}</span
        >
        <span class="num {item.tone ? `tone-${item.tone}` : ''}"
          >{item.value}</span
        >
      {/snippet}
    </DataTable>

    {#if summaryNote}
      <p class="hint note">{summaryNote}</p>
    {/if}
  {/if}
</AppShell>

<style>
  .balance {
    display: flex;
    flex-direction: column;
    gap: 2px;
    margin-bottom: 14px;
  }

  .figure {
    font-size: 24px;
    font-weight: 700;
    color: var(--ink);
    line-height: 1.15;
  }

  .error {
    color: var(--bad);
    margin: 0;
  }

  .note {
    margin: 0;
    font-size: 11px;
  }
</style>
