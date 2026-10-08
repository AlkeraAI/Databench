/** How far a person may rely on an item, independently of where the item originated. */
export type TrustGrade = "verified" | "agent" | "unverified";

const trustToken = (value: string): string => value.trim().toLowerCase();

/** Project the open wire pair onto the knowledge base's fail-closed trust standing. */
export function toTrustGrade(trust: string, source: string): TrustGrade {
  if (trustToken(trust) !== "trusted") return "unverified";
  return trustToken(source) === "human" ? "verified" : "agent";
}
