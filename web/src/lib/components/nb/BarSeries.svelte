<script>
  /**
   * BarSeries — a labelled bar chart with a real y-axis.
   *
   * `burst-timeline` draws the same data as a *sparkline*: percentage heights,
   * no axis, no units, and a `count` that is a count. That is right for "recent
   * bursts" and wrong for every screen that has to say *how much* — deepseek-
   * balance's three daily charts (spend, usage time, cost per hour) each need a
   * value axis in their own unit, and the day still to come is drawn as a
   * dashed projection rather than a solid bar.
   *
   * So this is a second, different animal, lifted from that page:
   *
   *   - the y-axis is computed here (`niceStep`), because a scale of
   *     `max × 4 / 4` labels 1, 2.5, 3.75 and no one reads that;
   *   - `bar.display` is the string the caller wants shown — "¥12.40", "2h 15m",
   *     "84 ¢/h" — while `bar.value` is the number that decides the height.
   *     Formatting belongs to the consumer (only it knows the currency); the
   *     geometry belongs to the block;
   *   - `bar.projected` draws the outline-and-dash bar: today's spend so far,
   *     extended to a full day. It is a different claim from a measured bar and
   *     it has to look like one;
   *   - a `null` value keeps its slot. Blank days are data ("we held no
   *     snapshots"), and dropping them would silently close a gap in the axis.
   *
   * `axisFormat` is a function for the same reason `adaptFacets` is on
   * filter-panel: the API returns numbers and the reader wants minutes, and a
   * block that insists on plain numbers pushes that conversion back into every
   * consumer.
   */
  let {
    title = "",
    note = "",
    unit = "",
    bars = [],
    height = 150,
    axisFormat = (v) => String(Math.round(v * 100) / 100),
  } = $props();

  // Only measured values set the scale. A projection that overshoots the axis
  // would flatten every real day to make room for a guess.
  const values = $derived(
    bars
      .filter((bar) => !bar.projected && bar.value != null)
      .map((bar) => Number(bar.value)),
  );
  const maxValue = $derived(Math.max(1, ...values));
  const step = $derived(niceStep(maxValue / 4));
  // Ticks sit on multiples of the step, not on `top / 4`: a top of 150 with a
  // step of 50 has three gridlines, and labelling them 37.5 / 75 / 112.5 would
  // be a scale no one reads.
  const top = $derived(Math.ceil(maxValue / step) * step);
  const ticks = $derived(Array.from({ length: Math.round(top / step) + 1 }, (_, i) => i * step));

  // The logical viewBox is sized from the number of bars so 14 days stay
  // readable and 90 days do not become slivers; `width: 100%` then scales it to
  // the card without a horizontal scrollbar.
  const slot = $derived(30);
  const plot = $derived({
    left: 46,
    right: 8,
    topPad: 10,
    bottom: 24,
    barWidth: Math.max(3, Math.min(slot * 0.62, 26)),
  });
  const geometry = $derived.by(() => {
    const plotWidth = Math.max(240, bars.length * slot);
    const width = plotWidth + plot.left + plot.right;
    const slotWidth = plotWidth / Math.max(1, bars.length);
    const heightOf = height;
    const y = (value) => plot.topPad + heightOf * (1 - value / top);
    const xOf = (index) => plot.left + index * slotWidth + slotWidth / 2;
    return { plotWidth, width, slotWidth, y, xOf, totalHeight: heightOf + plot.topPad + plot.bottom };
  });

  // At most nine date labels, or they collide into a grey smear.
  const labelEvery = $derived(Math.max(1, Math.ceil(bars.length / 9)));

  function niceStep(raw) {
    if (!(raw > 0)) return 1;
    const magnitude = 10 ** Math.floor(Math.log10(raw));
    const normalised = raw / magnitude;
    let chosen;
    if (normalised <= 1) chosen = 1;
    else if (normalised <= 2) chosen = 2;
    else if (normalised <= 2.5) chosen = 2.5;
    else if (normalised <= 5) chosen = 5;
    else chosen = 10;
    return chosen * magnitude;
  }

  function tooltip(bar) {
    const label = bar.projected ? `${bar.label} (projected)` : bar.label;
    return `${label} · ${bar.display ?? axisFormat(bar.value)}${unit ? ` ${unit}` : ""}`;
  }
</script>

<section class="bar-series">
  {#if title}
    <h3 class="series-title">
      {title}
      {#if unit}<span class="series-unit num">({unit})</span>{/if}
    </h3>
  {/if}
  {#if note}
    <p class="series-note hint">{note}</p>
  {/if}

  {#if values.length === 0}
    <p class="series-empty hint">No data in this range.</p>
  {:else}
    <div class="chart">
      <svg
        viewBox="0 0 {geometry.width} {geometry.totalHeight}"
        role="img"
        aria-label="{title || 'bar series'}{unit ? ` (${unit})` : ''}"
      >
        {#each ticks as value}
          <line
            x1={plot.left}
            y1={geometry.y(value)}
            x2={geometry.width - plot.right}
            y2={geometry.y(value)}
            class="grid"
            class:baseline={value === 0}
          />
          <text x={plot.left - 6} y={geometry.y(value) + 3} text-anchor="end" class="tick"
            >{axisFormat(value)}</text
          >
        {/each}

        {#each bars as bar, index (bar.label + index)}
          {@const centre = geometry.xOf(index)}
          {#if bar.value != null}
            {@const barHeight = Math.max(Number(bar.value) > 0 ? 1 : 0, (Number(bar.value) / top) * height)}
            <rect
              x={centre - plot.barWidth / 2}
              y={geometry.y(bar.value)}
              width={plot.barWidth}
              height={barHeight}
              rx="1.5"
              class="bar {bar.tone ?? 'accent'}"
              class:projected={bar.projected}
            >
              <title>{tooltip(bar)}</title>
            </rect>
          {/if}
          {#if bar.projected}
            <text x={centre} y={height + plot.topPad + 13} text-anchor="middle" class="tick today"
              >today</text
            >
          {:else if index % labelEvery === 0}
            <text x={centre} y={height + plot.topPad + 13} text-anchor="middle" class="tick"
              >{bar.label}</text
            >
          {/if}
        {/each}
      </svg>
    </div>
  {/if}
</section>

<style>
  .bar-series {
    margin-bottom: 16px;
  }

  .series-title {
    margin: 0 0 2px;
    font-size: 12.5px;
    font-weight: 600;
    color: var(--ink);
  }

  .series-unit {
    margin-left: 4px;
    font-weight: 500;
    font-size: 11px;
    color: var(--ink-muted);
  }

  .series-note {
    margin: 0 0 6px;
  }

  .series-empty {
    margin: 0;
    padding: 12px 0;
  }

  .chart {
    background: var(--bg-sunken);
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 8px 10px 4px;
  }

  svg {
    width: 100%;
    height: auto;
    display: block;
    font-family: inherit;
  }

  .grid {
    stroke: var(--line);
    stroke-width: 1;
  }

  .grid.baseline {
    stroke: var(--line-strong);
    stroke-width: 1.3;
  }

  .tick {
    fill: var(--neutral);
    font-size: 9px;
  }

  .tick.today {
    fill: var(--accent);
  }

  /* SVG fills take a class, not a `fill` attribute, so the series colour comes
     from the same tokens as everything else and a re-skin needs no edit here. */
  .bar.accent {
    fill: var(--accent);
  }
  .bar.ok {
    fill: var(--ok);
  }
  .bar.warn {
    fill: var(--warn);
  }
  .bar.bad {
    fill: var(--bad);
  }
  .bar.neutral {
    fill: var(--neutral);
  }

  /* A projection is a claim, not a measurement: hollow and dashed. */
  .bar.projected {
    fill: transparent;
    stroke: var(--accent);
    stroke-width: 1.4;
    stroke-dasharray: 4 3;
  }
</style>