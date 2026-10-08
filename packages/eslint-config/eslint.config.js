// The config package lints itself with its own rules.
import shared from "./index.js";

export default [...shared, { languageOptions: { globals: { process: "readonly" } } }];
