"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";

/**
 * One QueryClient per browser tab, created inside useState.
 *
 * NOT a module-level `new QueryClient()`: on the server a module global
 * is shared by every concurrent request, so one tenant's cached
 * documents could be handed to the next request that rendered. Creating
 * it in state scopes it to the component instance, which on the server
 * is one render and in the browser is one tab.
 */
export function QueryProvider({ children }: { children: React.ReactNode }) {
  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            // Tenant data. A long stale time means a document that
            // finished processing keeps showing as "processing", and
            // means data survives in memory longer than it needs to.
            staleTime: 10_000,
            retry: (failureCount, error) => {
              // Never retry an auth or permission failure: the answer
              // will not change, and each retry is another log line
              // that looks like an attack.
              const status = (error as { status?: number })?.status;
              if (status === 401 || status === 403 || status === 404) {
                return false;
              }
              return failureCount < 2;
            },
            refetchOnWindowFocus: false,
          },
        },
      }),
  );

  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}
