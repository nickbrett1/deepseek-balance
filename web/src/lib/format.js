/**
 * Presentation only: numbers into the strings a reader expects.
 *
 * Every rule here was in the inline `<script>` of the two pages this frontend
 * replaced, and they are kept as they were -- including the one that looks like
 * a bug: `integer()` rounds, `money()` does not zero-pad to two decimals. A
 * reader of a spend page compares magnitudes, and `12.4` is easier to scan than
 * `12.40` in a column of five.
 */

const CURRENCY_GAP = " ";

export function money(value, currency) {
  if (value == null || Number.isNaN(Number(value))) return "—";
  const text = Number(value).toLocaleString(undefined, {
    maximumFractionDigits: 2,
  });
  return currency ? text + CURRENCY_GAP + currency : text;
}

/** Money in a narrow chart tooltip: big numbers lose their decimals. */
export function moneyShort(value, currency) {
  if (value == null || Number.isNaN(Number(value))) return "—";
  const number = Number(value);
  const text =
    number >= 100
      ? number.toFixed(0)
      : number >= 10
        ? number.toFixed(1)
        : number.toFixed(2);
  return currency ? text + CURRENCY_GAP + currency : text;
}

/** Minutes as `2h 15m` / `45m`. */
export function duration(minutes) {
  if (minutes == null || Number.isNaN(Number(minutes))) return "—";
  const total = Math.round(Number(minutes));
  const hours = Math.floor(total / 60);
  return (hours ? hours + "h " : "") + (total % 60) + "m";
}

export function integer(value) {
  if (value == null || Number.isNaN(Number(value))) return "—";
  return Number(value).toLocaleString();
}

/**
 * The minor unit of a currency, for the cost-per-hour chart: fen for CNY, cents
 * otherwise. The original page had this mapping inline; it is a currency fact,
 * not a chart fact, so it lives with the other formatting.
 */
export function minorUnit(currency) {
  const code = (currency || "").toUpperCase();
  return code.startsWith("CNY") || code.startsWith("RMB") ? "分" : "¢";
}

export function minorInt(value) {
  return Math.round(value).toLocaleString();
}

/** A y-axis label for an amount: no trailing zeros, no thousands separator. */
export function axisNumber(value) {
  if (value >= 100) return String(Math.round(value));
  if (value >= 10) return String(Math.round(value * 10) / 10);
  return String(Math.round(value * 100) / 100);
}

/** A y-axis label for a duration in minutes: `45m`, `1.5h`. */
export function axisDuration(minutes) {
  const total = Math.round(minutes);
  if (total < 60) return total + "m";
  return Math.round((total / 60) * 10) / 10 + "h";
}

export function percent(part, whole) {
  if (!whole) return "0%";
  return Math.round(((part || 0) / whole) * 100) + "%";
}

/** `Nov 14 15:30` in the reader's locale, from an ISO timestamp. */
export function whenCell(iso) {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "—";
  return (
    date.toLocaleDateString([], { month: "short", day: "numeric" }) +
    " " +
    date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
  );
}

/** `15:30 · Nov 14`, the widget's "updated" line. */
export function updatedLine(iso) {
  if (!iso) return "no snapshot";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "no snapshot";
  return (
    date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) +
    " · " +
    date.toLocaleDateString([], { month: "short", day: "numeric" })
  );
}

/** `11-14` — the chart's x-axis label, from a `YYYY-MM-DD` day. */
export function dayLabel(day) {
  return (day || "").slice(5);
}

export function dayDate(day) {
  return (day || "").slice(0, 10);
}
