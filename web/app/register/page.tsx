import type { Metadata } from "next";

import { AuthLayout } from "@/components/auth/AuthLayout";
import { site } from "@/lib/site";

export const metadata: Metadata = {
  title: `Create an organization — ${site.name}`,
};

export default function RegisterPage() {
  return <AuthLayout mode="register" />;
}
