import type { Metadata } from "next";

import { ChatScreen } from "@/components/app/chat/ChatScreen";
import { site } from "@/lib/site";

export const metadata: Metadata = { title: `Chat — ${site.name}` };

export default function ChatPage() {
  return <ChatScreen />;
}
