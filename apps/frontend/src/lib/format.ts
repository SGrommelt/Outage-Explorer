/** Exact decimal display: no float conversion, rounding, or loss of tiny gaps. */
export function formatDecimal(value: string | number | null): string {
  if (value === null) return "Unavailable";
  const text = String(value);
  if (!/^-?\d+(\.\d+)?$/.test(text)) return text;
  const [integer, fraction = ""] = text.split(".");
  const tail = fraction.replace(/0+$/, "");
  return integer.replace(/\B(?=(\d{3})+(?!\d))/g, ",") + (tail ? `.${tail}` : "");
}
export function formatPercent(value: string | null): string {
  return value === null ? "Unavailable" : `${formatDecimal(value)}%`;
}
