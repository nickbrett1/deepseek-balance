<script>
  /**
   * Two screens, one bundle.
   *
   * The view is chosen from the pathname, not a hash: `/history` is a real URL
   * the widget links to, and the analysis table's "older" paging keeps the
   * address stable. A hash router would have made `/history` a fragment, which
   * Homepage's iframe (and a phone's back button) do not treat as a page.
   */
  import Widget from "$lib/views/Widget.svelte";
  import History from "$lib/views/History.svelte";

  const path =
    typeof window === "undefined"
      ? "/"
      : window.location.pathname.replace(/\/+$/, "") || "/";
  const view = path === "/history" ? "history" : "widget";
</script>

{#if view === "history"}
  <History />
{:else}
  <Widget />
{/if}
