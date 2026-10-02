type Tone = 'auto' | 'neutral' | 'info' | 'success' | 'caution' | 'warning' | 'critical';

export function StatTile({
  label,
  value,
  tone = 'auto',
  help,
  href,
}: {
  label: string;
  value: number | string;
  tone?: Tone;
  help?: string;
  /** When set, the whole tile links to a (filtered) list. */
  href?: string;
}) {
  const content = (
    <s-stack gap="small-200">
      <s-text color="subdued">{label}</s-text>
      <s-text tone={tone} fontSize="large-100" fontWeight="bold" fontVariantNumeric="tabular-nums">
        {value}
      </s-text>
      {help && <s-text color="subdued">{help}</s-text>}
    </s-stack>
  );
  if (href) {
    return (
      <s-clickable
        href={href}
        padding="base"
        background="base"
        border="base"
        borderRadius="base"
        accessibilityLabel={`${label}: ${value}`}
      >
        {content}
      </s-clickable>
    );
  }
  return (
    <s-box padding="base" background="base" border="base" borderRadius="base">
      {content}
    </s-box>
  );
}
