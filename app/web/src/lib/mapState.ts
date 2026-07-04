// Immutable helpers for ReadonlyMap-backed row state used by the inline-table
// manager pages. Returning the same reference when nothing changes keeps React
// state updates from re-rendering rows that did not move.

export function setMapValue<TValue>(
  current: ReadonlyMap<string, TValue>,
  key: string,
  value: TValue,
): ReadonlyMap<string, TValue> {
  const next = new Map(current);
  next.set(key, value);
  return next;
}

export function clearMapValue<TValue>(
  current: ReadonlyMap<string, TValue>,
  key: string,
): ReadonlyMap<string, TValue> {
  if (!current.has(key)) return current;
  const next = new Map(current);
  next.delete(key);
  return next;
}
