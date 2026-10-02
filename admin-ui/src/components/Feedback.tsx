import type { ReactNode } from 'react';

export function ErrorBanner({
  error,
  heading = 'Something went wrong',
  onRetry,
}: {
  error: string | null | undefined;
  heading?: string;
  onRetry?: () => void;
}) {
  if (!error) return null;
  return (
    <s-banner tone="critical" heading={heading}>
      <s-paragraph>{error}</s-paragraph>
      {onRetry && (
        <s-button slot="secondary-actions" onClick={onRetry}>
          Retry
        </s-button>
      )}
    </s-banner>
  );
}

export function SuccessBanner({ message, onDismiss }: { message: string | null | undefined; onDismiss?: () => void }) {
  if (!message) return null;
  return (
    <s-banner tone="success" dismissible onDismiss={onDismiss}>
      <s-paragraph>{message}</s-paragraph>
    </s-banner>
  );
}

/** Error + success banners from `useNotice()`. */
export function NoticeBanners({
  error,
  success,
  onClear,
}: {
  error: string | null;
  success: string | null;
  onClear: () => void;
}) {
  if (!error && !success) return null;
  return (
    <s-stack gap="base">
      <ErrorBanner error={error} />
      <SuccessBanner message={success} onDismiss={onClear} />
    </s-stack>
  );
}

export function Loading({ label = 'Loading' }: { label?: string }) {
  return (
    <s-box padding="large-200">
      <s-stack alignItems="center" gap="base">
        <s-spinner accessibilityLabel={label} size="large" />
        <s-text color="subdued">{label}…</s-text>
      </s-stack>
    </s-box>
  );
}

export function EmptyMessage({ heading, children }: { heading: string; children?: ReactNode }) {
  return (
    <s-box padding="large-200">
      <s-stack alignItems="center" gap="small">
        <s-heading>{heading}</s-heading>
        {children && <s-paragraph color="subdued">{children}</s-paragraph>}
      </s-stack>
    </s-box>
  );
}
