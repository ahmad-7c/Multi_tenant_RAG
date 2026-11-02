import { notFound } from "next/navigation";

import { DesignGallery } from "./DesignGallery";

/**
 * The component reference, for fidelity review.
 *
 * 404s in production. It is excluded by a runtime check rather than by
 * a build flag because a flag that has to be set correctly at deploy
 * time is a flag that will one day be wrong -- and this page renders
 * every component with SAMPLE data, which is exactly what must never
 * appear in a product that forbids invented statistics.
 */
export default function DesignPage() {
  if (process.env.NODE_ENV === "production") notFound();
  return <DesignGallery />;
}
