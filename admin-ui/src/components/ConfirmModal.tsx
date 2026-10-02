import { useImperativeHandle, useRef, useState, type ReactNode, type Ref } from 'react';

export interface ConfirmModalHandle {
  open: () => void;
  close: () => void;
}

/**
 * Confirmation dialog built on `<s-modal>`. Open it imperatively through the `ref` handle
 * (`ref.current.open()`); the confirm button awaits `onConfirm` and closes on success.
 */
export function ConfirmModal({
  ref,
  id,
  heading,
  confirmLabel,
  destructive = false,
  onConfirm,
  children,
}: {
  ref: Ref<ConfirmModalHandle>;
  id: string;
  heading: string;
  confirmLabel: string;
  destructive?: boolean;
  onConfirm: () => Promise<boolean | void>;
  children: ReactNode;
}) {
  const modalRef = useRef<HTMLElementTagNameMap['s-modal']>(null);
  const [busy, setBusy] = useState(false);

  useImperativeHandle(ref, () => ({
    open: () => modalRef.current?.showOverlay(),
    close: () => modalRef.current?.hideOverlay(),
  }));

  const confirm = async () => {
    setBusy(true);
    try {
      const keepOpen = (await onConfirm()) === false;
      if (!keepOpen) modalRef.current?.hideOverlay();
    } finally {
      setBusy(false);
    }
  };

  return (
    <s-modal ref={modalRef} id={id} heading={heading}>
      {children}
      <s-button
        slot="primary-action"
        variant="primary"
        tone={destructive ? 'critical' : 'auto'}
        loading={busy}
        onClick={() => void confirm()}
      >
        {confirmLabel}
      </s-button>
      <s-button slot="secondary-actions" commandFor={id} command="--hide" disabled={busy}>
        Cancel
      </s-button>
    </s-modal>
  );
}
