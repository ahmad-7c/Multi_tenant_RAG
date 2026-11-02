import type { Metadata } from "next";

import { AuthLayout } from "@/components/auth/AuthLayout";
import { site } from "@/lib/site";

export const metadata: Metadata = {
  title: `Sign in — ${site.name}`,
};

export default function LoginPage() {
  return <AuthLayout mode="login" />;
}
