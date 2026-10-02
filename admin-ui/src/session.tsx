import { createContext, useContext, type ReactNode } from 'react';
import { api } from './api';
import { useApi } from './hooks/useApi';
import type { SessionInfo } from './types';

interface SessionContextValue {
  session: SessionInfo | null;
  loading: boolean;
  error: string | null;
  reload: () => Promise<void>;
}

const SessionContext = createContext<SessionContextValue>({
  session: null,
  loading: true,
  error: null,
  reload: async () => {},
});

export function SessionProvider({ children }: { children: ReactNode }) {
  const { data, loading, error, reload } = useApi(api.session);
  return (
    <SessionContext.Provider value={{ session: data, loading, error, reload }}>{children}</SessionContext.Provider>
  );
}

export function useSession(): SessionContextValue {
  return useContext(SessionContext);
}
