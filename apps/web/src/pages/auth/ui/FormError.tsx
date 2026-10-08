import { Callout } from "@alkera/ui";

/** The form-level error banner: a danger Callout that renders only when there's an
 *  error, so pages don't repeat the null-check at every call site. */
export function FormError({ title, error }: { title: string; error: string | null }) {
  if (!error) return null;
  return (
    <Callout tone="danger" title={title}>
      {error}
    </Callout>
  );
}
