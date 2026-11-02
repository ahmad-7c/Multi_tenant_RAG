"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { createContext, useCallback, useContext, useEffect } from "react";

import { apiFetch, setUnauthorizedHandler } from "@/lib/api";
import type { User } from "@/lib/types";

const UserContext = createContext<User | null>(null);

/**
 * The signed-in user, handed down from the server layout.
 *
 * Client components need `org_id` and `id` to build query keys, and
 * fetching /users/me again from the browser to learn what the server
 * already knew would be a second round trip on every page load.
 */
export function useUser(): User {
  const user = useContext(UserContext);
  if (!user) throw new Error("useUser must be used inside <UserProvider>");
  return user;
}

export function UserProvider({
  user,
  children,
}: {
  user: User;
  children: React.ReactNode;
}) {
  const queryClient = useQueryClient();
  const router = useRouter();

  useEffect(() => {
    // Any 401 from anywhere drops every cached response before the
    // redirect. Query keys are namespaced by org and user, but keys
    // are not a security boundary -- the data is still in memory, and
    // the next person to use this browser must not be able to see it
    // in a back-navigation.
    setUnauthorizedHandler(() => {
      queryClient.clear();
      router.replace("/login");
    });
    return () => setUnauthorizedHandler(() => {});
  }, [queryClient, router]);

  return <UserContext.Provider value={user}>{children}</UserContext.Provider>;
}

/** Sign out: clear the cookie, then clear everything held locally. */
export function useSignOut() {
  const queryClient = useQueryClient();
  const router = useRouter();

  return useCallback(async () => {
    try {
      await apiFetch("auth/logout", { method: "POST" });
    } finally {
      // In `finally`: if the logout call fails the user still intended
      // to leave, and leaving their org's documents in a warm cache
      // because a network request failed is the wrong failure.
      queryClient.clear();
      router.replace("/login");
      router.refresh();
    }
  }, [queryClient, router]);
}
