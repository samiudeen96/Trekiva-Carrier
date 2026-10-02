/** Small building blocks for list pages: debounced search, carrier select, pagination summary. */
import { useEffect, useRef, useState } from 'react';
import type { Carrier } from '../types';

const SEARCH_DEBOUNCE_MS = 400;

/** Search field that reports its (trimmed) value after the user pauses typing. */
export function SearchInput({
  label,
  placeholder,
  value,
  onSearch,
}: {
  label: string;
  placeholder?: string;
  /** The applied search (e.g. from the URL); external changes reset the field. */
  value: string;
  onSearch: (value: string) => void;
}) {
  const [text, setText] = useState(value);
  const onSearchRef = useRef(onSearch);
  onSearchRef.current = onSearch;

  useEffect(() => setText(value), [value]);

  useEffect(() => {
    if (text.trim() === value) return;
    const timer = setTimeout(() => onSearchRef.current(text.trim()), SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [text]);

  return (
    <s-search-field
      label={label}
      placeholder={placeholder}
      value={text}
      autocomplete="off"
      onInput={(e) => setText(e.currentTarget.value)}
    />
  );
}

/** Carrier display name, falling back to the code (e.g. for carriers no longer registered). */
export function carrierName(code: string | null | undefined, carriers: readonly Carrier[] | null | undefined): string {
  if (!code) return '—';
  return carriers?.find((c) => c.code === code)?.display_name ?? code;
}

export function CarrierFilterSelect({
  carriers,
  value,
  onChange,
}: {
  carriers: readonly Carrier[] | null;
  value: string;
  onChange: (value: string) => void;
}) {
  // Keep an unknown carrier from the URL selectable so the select shows the applied filter.
  const known = carriers?.some((c) => c.code === value) ?? false;
  return (
    <s-select label="Carrier" value={value} onChange={(e) => onChange(e.currentTarget.value)}>
      <s-option value="">All carriers</s-option>
      {value && !known && <s-option value={value}>{value}</s-option>}
      {(carriers ?? []).map((carrier) => (
        <s-option key={carrier.code} value={carrier.code}>
          {carrier.display_name}
        </s-option>
      ))}
    </s-select>
  );
}

/** Props for `<s-table paginate>`. */
export interface TablePagination {
  hasPreviousPage: boolean;
  hasNextPage: boolean;
  onPreviousPage: () => void;
  onNextPage: () => void;
}

/** "Showing 51–100 of 230". */
export function PageSummary({ offset, count, total }: { offset: number; count: number; total: number }) {
  if (total === 0 || count === 0) return null;
  return (
    <s-text color="subdued">
      Showing {offset + 1}–{offset + count} of {total}
    </s-text>
  );
}

/** Read-only, scrollable JSON view (carrier request/response payloads, log data). */
export function JsonBlock({ value }: { value: unknown }) {
  const text = value === undefined ? 'undefined' : JSON.stringify(value, null, 2);
  return (
    <s-box padding="small" background="subdued" borderRadius="base">
      <pre
        style={{
          margin: 0,
          maxHeight: '320px',
          overflow: 'auto',
          whiteSpace: 'pre-wrap',
          wordBreak: 'break-word',
          fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
          fontSize: '12px',
          lineHeight: 1.45,
        }}
      >
        {text}
      </pre>
    </s-box>
  );
}

/** Collapsible JSON (native `<details>`), collapsed by default. */
export function JsonDetails({ summary, value }: { summary: string; value: unknown }) {
  return (
    <details>
      <summary style={{ cursor: 'pointer' }}>
        <s-text color="subdued">{summary}</s-text>
      </summary>
      <s-box paddingBlockStart="small-200">
        <JsonBlock value={value} />
      </s-box>
    </details>
  );
}
