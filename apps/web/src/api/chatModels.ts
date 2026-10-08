// The models this reader may start a chat on, for a surface that is not the
// composer.
//
// The chat composer reads the same catalog through its data source, because the
// composer is written against one contract for two shells. The preferences page
// has no data source — it is an ordinary portal page — so it reads the route
// directly, under the same key.

import { useQuery, type UseQueryResult } from "@tanstack/react-query";

import { chatModels, type ChatModelRead } from "./cloudChat/transport";
import { keys } from "./keys";

export type { ChatModelRead };

/** The gateway catalog. An EMPTY list means it could not be read, not that the
 *  reader has no models — a caller must never treat it as "reset the default". */
export function useMyChatModels(): UseQueryResult<ChatModelRead[]> {
  return useQuery({
    queryKey: keys.me.chatModels,
    staleTime: 5 * 60_000,
    queryFn: async () => (await chatModels()).items ?? [],
  });
}
