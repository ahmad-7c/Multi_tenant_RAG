"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { useState } from "react";

import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/primitives";
import { ApiError, apiFetch } from "@/lib/api";

type Mode = "login" | "register";

/**
 * Sign in / create an organization.
 *
 * Posts to the BFF, never to the API. The response body is the user
 * profile; the token is set as an httpOnly cookie the browser cannot
 * read, so there is nothing here to store and nothing to clear beyond
 * the query cache.
 */
export function AuthForm({ mode }: { mode: Mode }) {
  const router = useRouter();
  const params = useSearchParams();
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function onSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    setPending(true);

    const data = new FormData(event.currentTarget);
    const payload = {
      org_name: String(data.get("org_name") ?? "").trim(),
      email: String(data.get("email") ?? "").trim(),
      password: String(data.get("password") ?? ""),
    };

    try {
      await apiFetch(`auth/${mode}`, {
        method: "POST",
        body: JSON.stringify(payload),
      });
      // Only a PATH is taken from the query string. Accepting a full
      // URL here would make this an open redirect: ?next=https://evil
      // would send a freshly-authenticated user off-site.
      const next = params.get("next");
      const target = next && next.startsWith("/") && !next.startsWith("//") ? next : "/chat";
      // refresh() re-runs the server components so the new cookie is
      // reflected before the navigation paints.
      router.replace(target);
      router.refresh();
    } catch (caught) {
      setError(
        caught instanceof ApiError
          ? caught.message
          : "Something went wrong. Try again.",
      );
      setPending(false);
    }
  }

  const isRegister = mode === "register";

  return (
    <form onSubmit={onSubmit} className="flex flex-col gap-4" noValidate>
      {error && (
        // assertive: the user is about to retype a password and needs
        // to know the last attempt failed before they start.
        <p
          role="alert"
          className="rounded-ctl border border-[color:var(--err)]/35 bg-[color:var(--err)]/10 px-3.5 py-3 text-[13px] text-err-tx"
        >
          {error}
        </p>
      )}

      <Input
        label="Organization"
        name="org_name"
        required
        // `organization` is the standard token. Not "off": suppressing
        // a password manager on a real sign-in form pushes people
        // towards weaker, memorable passwords.
        autoComplete="organization"
        placeholder="acme-corp"
        hint={
          isRegister
            ? "This creates a new organization. The name must be unique."
            : undefined
        }
      />
      <Input
        label="Email"
        name="email"
        type="email"
        required
        autoComplete={isRegister ? "email" : "username"}
      />
      <Input
        label="Password"
        name="password"
        type="password"
        required
        minLength={8}
        autoComplete={isRegister ? "new-password" : "current-password"}
        hint={isRegister ? "At least 8 characters." : undefined}
      />

      <Button type="submit" disabled={pending} className="mt-2 h-12 w-full">
        {pending
          ? isRegister
            ? "Creating…"
            : "Signing in…"
          : isRegister
            ? "Create organization"
            : "Sign in"}
        {!pending && (
          <svg viewBox="0 0 20 20" className="h-4 w-4" aria-hidden="true">
            <path
              d="M4 10h11M11 5.5 15.5 10 11 14.5"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.8"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
        )}
      </Button>
    </form>
  );
}
