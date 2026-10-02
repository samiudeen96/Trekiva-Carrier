import { useCallback, useMemo } from 'react';
import { useSearchParams } from 'react-router-dom';

export interface SearchFilters<K extends string> {
  /** Current filter values from the URL (missing -> ''). */
  values: Record<K, string>;
  /** Current page offset from the URL (`offset`, default 0). */
  offset: number;
  /** Sets one filter (empty removes it) and resets pagination. */
  setFilter: (key: K, value: string) => void;
  setOffset: (offset: number) => void;
  /** Removes every filter and the offset. */
  clear: () => void;
  /** True when any filter is set. */
  active: boolean;
}

/**
 * List filters kept in the URL query string, so filtered views can be linked to (e.g. the
 * Dashboard's "Failed" tile -> `/orders?status=FAILED`) and survive reloads.
 */
export function useSearchFilters<K extends string>(keys: readonly K[]): SearchFilters<K> {
  const [params, setParams] = useSearchParams();
  const search = params.toString();

  const values = useMemo(() => {
    const result = {} as Record<K, string>;
    for (const key of keys) result[key] = params.get(key) ?? '';
    return result;
    // `keys` is a module-level constant at every call site.
  }, [search]);

  const offset = Math.max(0, Math.floor(Number(params.get('offset')) || 0));

  const setFilter = useCallback(
    (key: K, value: string) =>
      setParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          if (value) next.set(key, value);
          else next.delete(key);
          next.delete('offset');
          return next;
        },
        { replace: true },
      ),
    [setParams],
  );

  const setOffset = useCallback(
    (value: number) =>
      setParams((prev) => {
        const next = new URLSearchParams(prev);
        if (value > 0) next.set('offset', String(value));
        else next.delete('offset');
        return next;
      }),
    [setParams],
  );

  const clear = useCallback(() => setParams(new URLSearchParams(), { replace: true }), [setParams]);

  const active = keys.some((key) => values[key] !== '');

  return { values, offset, setFilter, setOffset, clear, active };
}
