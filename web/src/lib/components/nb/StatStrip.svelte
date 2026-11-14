<script>
  /**
   * StatStrip — topcards stat summary row.
   *
   * Displays KPI cards (label, big tabular-figure value, sub-line) in a
   * responsive grid. Lifted from deepseek-balance and stratechery-search.
   */
  let { stats = [] } = $props();
</script>

<div class="stat-strip" style="--stat-cols: {stats.length};">
  {#each stats as stat}
    <div class="card stat-card">
      <div class="stat-label micro">{stat.label}</div>
      <div class="stat-value num {stat.tone ?? ''}">{stat.value}</div>
      {#if stat.sub}
        <div class="stat-sub hint">{stat.sub}</div>
      {/if}
    </div>
  {/each}
</div>

<style>
  .stat-strip {
    display: grid;
    grid-template-columns: repeat(var(--stat-cols, 3), 1fr);
    gap: 12px;
    margin-bottom: 20px;
  }

  .stat-card {
    padding: 12px 14px;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  .stat-label {
    margin: 0;
  }

  .stat-value {
    font-size: 20px;
    font-weight: 700;
    line-height: 1.2;
    color: var(--ink);
  }

  .stat-value.ok {
    color: var(--ok);
  }
  .stat-value.warn {
    color: var(--warn);
  }
  .stat-value.bad {
    color: var(--bad);
  }
  .stat-value.info {
    color: var(--accent);
  }

  .stat-sub {
    margin: 0;
  }

  @media (max-width: 600px) {
    .stat-strip {
      grid-template-columns: 1fr;
      gap: 8px;
    }
  }
</style>