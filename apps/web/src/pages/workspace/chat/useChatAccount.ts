// Who the chrome says is signed in, from whichever shell the chat is running in.
//
// The extension learns this from the daemon's auth channel and the browser from
// its own session read, so the composition asks the host rather than either
// source: a snapshot now, plus a subscription for the moment the shell learns
// more (a daemon that has only just reported, a session that just refreshed).

import { useEffect, useState } from "react";

import { chatHost } from "./data";
import type { ChatAccount } from "./data";

export function useChatAccount(): ChatAccount {
  const host = chatHost();
  const [account, setAccount] = useState<ChatAccount>(() => host.account());
  useEffect(() => host.onAccountChange(() => setAccount(host.account())), [host]);
  return account;
}
