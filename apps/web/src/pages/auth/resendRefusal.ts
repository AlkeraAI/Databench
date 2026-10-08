import type { RefusalWording } from "../../api/errors";

/** How a refused verification-email send reads, on every page that offers one. The mail
 *  relay failing is a 503 the server names `email_send_failed`; it has its own sentence so
 *  the reader knows to wait, where any other 5xx reads as the fallback. */
export const RESEND_REFUSAL: RefusalWording = {
  known: { email_send_failed: "We couldn't send the verification email. Try again in a few minutes." },
  fallback: "Could not send a verification email.",
};
