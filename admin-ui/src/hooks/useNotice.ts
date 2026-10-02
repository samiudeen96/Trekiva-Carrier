import { useCallback, useState } from 'react';
import { errorMessage } from '../api';
import { toast } from '../appBridge';

export interface Notice {
  /** Error text to show in a critical banner. */
  error: string | null;
  /** Success text to show in a success banner (only when no admin toast could be shown). */
  success: string | null;
  showError: (err: unknown) => void;
  showSuccess: (message: string) => void;
  clear: () => void;
}

/**
 * Page-level feedback. Errors are always rendered as a banner (and toasted when embedded);
 * successes use an App Bridge toast when available, falling back to a banner in dev mode.
 */
export function useNotice(): Notice {
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);

  const showError = useCallback((err: unknown) => {
    const message = errorMessage(err);
    setSuccess(null);
    setError(message);
    toast(message, true);
  }, []);

  const showSuccess = useCallback((message: string) => {
    setError(null);
    setSuccess(toast(message) ? null : message);
  }, []);

  const clear = useCallback(() => {
    setError(null);
    setSuccess(null);
  }, []);

  return { error, success, showError, showSuccess, clear };
}
