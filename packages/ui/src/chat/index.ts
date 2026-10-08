// The redesigned chat frontend, organized by concern. Each concern folder
// holds its components, css, and index, and everything re-exports from here.
// Presentation only. State arrives as props over @alkera/chat-model shapes,
// never fetched here.
//
// The host renders everything inside a `.chat-root` element, sets
// `data-theme="dark"` on it for dark, and imports `@alkera/ui/chat/styles`
// once for the token + base layer.

export * from "./transcript";
export * from "./prose";
export * from "./indicators";
export * from "./syntax";
export * from "./tools";
export * from "./activity";
export * from "./subagent";
export * from "./panel";
export * from "./home";
export * from "./composer";
export * from "./interrupts";
export * from "./plan";
export * from "./nav";
export * from "./details";
