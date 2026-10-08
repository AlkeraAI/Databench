/** First letter uppercased, the rest untouched ("pass" -> "Pass"). The wire speaks lowercase
 *  codes and snake_case keys; a reader never should. Empty input stays empty. */
export function capitalize(value: string): string {
  return value.charAt(0).toUpperCase() + value.slice(1);
}
