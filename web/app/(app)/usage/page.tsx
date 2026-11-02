import type { Metadata } from "next";
import { notFound } from "next/navigation";

import { UsageScreen } from "@/components/app/usage/UsageScreen";
import { requireUser } from "@/lib/server-api";
import { site } from "@/lib/site";

export const metadata: Metadata = { title: `Usage — ${site.name}` };

/**
 * Admin only.
 *
 * Checked here as well as in the sidebar (which hides the link) and in
 * the backend (which returns 403). Three layers for one rule, because
 * hiding a link is not access control and a member typing the URL
 * should not reach a screen that renders an error -- they should not
 * reach the screen.
 */
export default async function UsagePage() {
  const user = await requireUser();
  if (user.role !== "admin") notFound();
  return <UsageScreen />;
}
