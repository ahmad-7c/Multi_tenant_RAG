import type { Metadata } from "next";

import { DocumentsScreen } from "@/components/app/documents/DocumentsScreen";
import { site } from "@/lib/site";

export const metadata: Metadata = { title: `Documents — ${site.name}` };

export default function DocumentsPage() {
  return <DocumentsScreen />;
}
