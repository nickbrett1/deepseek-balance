<script>
  /**
   * DataTable — a column-aligned table of records.
   *
   * Generalised from data-download, which has two of them: the request book
   * (id · state · type · estimate · age · why it matters) and the plan-type
   * parameter schema (parameter · type · constraints · what it means). Both
   * wanted the same three things that a ranked list does not give:
   *
   *   - columns that line up down the page, so `$0.0412` sits under `$0.0412`
   *     rather than wherever the previous cell ended;
   *   - numeric columns that are right-aligned and tabular, which is the whole
   *     reason a table beats a list of sentences;
   *   - header labels that belong to the columns rather than repeating in every
   *     row.
   *
   * `row(item, index)` returns the cells. The block owns the shell — the card,
   * the header row, the hairlines, the empty state — and the consumer owns what
   * is *in* each cell, because that is where the two screens differ: a state
   * pill with a tooltip versus a list of schema constraints.
   *
   * Below 600px the header row is hidden and each row wraps. A six-column table
   * on a phone is a horizontal scrollbar or a truncated id; wrapping with the
   * header dropped keeps the values readable and lets the cells that matter
   * (the state, the reason) sit on their own line. The header is only worth the
   * vertical space when there is room for it.
   */
  let {
    columns = [],
    items = [],
    title = "",
    titleTag = "div",
    emptyMessage = "Nothing to display.",
    row,
  } = $props();

  // Column widths are the caller's business (they know whether their first
  // column is an id or prose), but the fallback has to be honest: equal columns
  // would make a 40-character id fight a 4-character age for the same space.
  const template = $derived(
    columns
      .map((column) => column.width ?? (column.align === "end" ? "7rem" : "1fr"))
      .join(" "),
  );
</script>

<div class="data-table-wrap">
  {#if title}
    <svelte:element this={titleTag} class="section-title">
      {title} ({items.length})
    </svelte:element>
  {/if}

  {#if items.length === 0}
    <div class="card empty-card hint">{emptyMessage}</div>
  {:else}
    <div class="card table-card">
      <div class="tbl" style="--cols: {template}">
        <div class="tbl-head">
          {#each columns as column}
            <span class="col-label micro" class:end={column.align === "end"}
              >{column.label}</span
            >
          {/each}
        </div>
        {#each items as item, index (item.id ?? index)}
          <div class="tbl-row">
            {@render row?.(item, index)}
          </div>
        {/each}
      </div>
    </div>
  {/if}
</div>

<style>
  .table-card {
    padding: 0;
    overflow: hidden;
  }

  .tbl {
    display: flex;
    flex-direction: column;
  }

  .tbl-head,
  .tbl-row {
    display: grid;
    grid-template-columns: var(--cols);
    gap: 12px;
    align-items: baseline;
    padding: 7px 14px;
  }

  .tbl-head {
    background: var(--bg-sunken);
    border-bottom: 1px solid var(--line);
  }

  .tbl-row + .tbl-row {
    border-top: 1px solid var(--line);
  }

  .tbl-row:hover {
    background: var(--bg-hover);
  }

  /* Every cell, whoever wrote it: one line, truncated, so a long id cannot
     push the numeric columns out of alignment. A cell that genuinely needs to
     wrap opts out with `.wrap` (the block styles it, the consumer names it). */
  .tbl-row :global(*) {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .tbl-row :global(.num) {
    text-align: right;
    font-variant-numeric: tabular-nums;
  }

  .tbl-row :global(.wrap) {
    white-space: normal;
    overflow: visible;
  }

  .col-label.end {
    text-align: right;
  }

  .empty-card {
    padding: 14px;
  }

  @media (max-width: 600px) {
    /* One record per block, header gone: the alternative was a 1,300px-wide
       scroll area on a 390px screen. */
    .tbl-head {
      display: none;
    }

    .tbl-row {
      display: flex;
      flex-wrap: wrap;
      gap: 4px 10px;
      padding: 9px 12px;
    }

    .tbl-row :global(.num) {
      text-align: left;
    }
  }
</style>