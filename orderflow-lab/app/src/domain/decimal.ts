// Fixed-point decimal on BigInt (18 places) for fills and money, so 0.1 + 0.2 stays 0.3.
const SCALE = 18n;
const ONE = 10n ** SCALE;

export type Dec = bigint;

export function dec(v: string | number): Dec {
  const s = typeof v === "number" ? v.toString() : v.trim();
  if (!/^[-+]?(\d+\.?\d*|\.\d+)(e[-+]?\d+)?$/i.test(s)) throw new Error(`not a number: ${v}`);
  if (/e/i.test(s)) return dec(Number(s).toFixed(18));
  const neg = s.startsWith("-");
  const [i, f = ""] = s.replace(/^[-+]/, "").split(".");
  const frac = (f + "0".repeat(Number(SCALE))).slice(0, Number(SCALE));
  const r = BigInt(i || "0") * ONE + BigInt(frac || "0");
  return neg ? -r : r;
}

export const ZERO: Dec = 0n;
export const add = (a: Dec, b: Dec) => a + b;
export const sub = (a: Dec, b: Dec) => a - b;
export const mul = (a: Dec, b: Dec) => (a * b) / ONE;
/** division rounded half away from zero at 18 places */
export function div(a: Dec, b: Dec): Dec {
  if (b === 0n) throw new Error("division by zero");
  const q = (a * ONE * 2n) / b;
  return q >= 0n ? (q + 1n) / 2n : (q - 1n) / 2n;
}
export const neg = (a: Dec) => -a;
export const cmp = (a: Dec, b: Dec) => (a < b ? -1 : a > b ? 1 : 0);
export const isZero = (a: Dec) => a === 0n;

/** round half away from zero to `places` and print without exponent */
export function toFixed(a: Dec, places = 2): string {
  const p = 10n ** (SCALE - BigInt(places));
  const r = a >= 0n ? (a + p / 2n) / p : (a - p / 2n) / p;
  const s = (r < 0n ? -r : r).toString().padStart(places + 1, "0");
  const out = places ? `${s.slice(0, -places)}.${s.slice(-places)}` : s;
  return (r < 0n ? "-" : "") + out;
}

export const toNumber = (a: Dec) => Number(toFixed(a, 12));
