/**
 * Join class names, dropping falsy entries.
 *
 * Deliberately not `clsx` or `tailwind-merge`: this is six lines, and
 * every dependency in a UI bundle is a supply-chain surface. There is
 * no class CONFLICT resolution here -- the components below never pass
 * two competing utilities for the same property, and a caller's
 * className always comes last so it wins by source order.
 */
export function cx(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(" ");
}
