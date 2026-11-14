/**
 * The single place the frontend talks to FastAPI.
 *
 * There is no token here, unlike data-download: this app is reachable only over
 * Tailscale, and its auth story is the network. So the only thing this module
 * owns is the *time* the page must agree with the server about.
 */

/**
 * The viewer's local time, with an offset, as an ISO-8601 string.
 *
 * Every endpoint takes `now` because "today" is a local day: the container runs
 * in UTC, the reader does not. `toISOString()` would send Z and quietly shift
 * the day boundary by however many hours the reader is from Greenwich.
 */
export function localIso(date = new Date()) {
  const pad = (n) => String(n).padStart(2, "0");
  const offset = -date.getTimezoneOffset(); // minutes east of UTC
  const sign = offset >= 0 ? "+" : "-";
  const abs = Math.abs(offset);
  return (
    date.getFullYear() +
    "-" +
    pad(date.getMonth() + 1) +
    "-" +
    pad(date.getDate()) +
    "T" +
    pad(date.getHours()) +
    ":" +
    pad(date.getMinutes()) +
    ":" +
    pad(date.getSeconds()) +
    "." +
    String(date.getMilliseconds()).padStart(3, "0") +
    sign +
    pad(Math.floor(abs / 60)) +
    ":" +
    pad(abs % 60)
  );
}

export async function getJson(path, { signal } = {}) {
  const response = await fetch(path, {
    headers: { Accept: "application/json" },
    signal,
  });
  if (!response.ok) {
    const error = new Error(
      response.status === 503
        ? "The frontend is not built. Run `npm run build` in web/."
        : `The server answered ${response.status}.`,
    );
    error.status = response.status;
    throw error;
  }
  return response.json();
}

/** `/ui/history.json?days=14&now=…`, with the viewer's clock. */
export function historyPath({ days = 14, now = localIso() } = {}) {
  return `/ui/history.json?days=${days}&now=${encodeURIComponent(now)}`;
}

export function widgetPath({ now = localIso() } = {}) {
  return `/ui/widget.json?now=${encodeURIComponent(now)}`;
}
