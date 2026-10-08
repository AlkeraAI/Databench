// The notebook settings schema and its validator, with nothing of the editor:
// a caller that only reads settings (the live notebook document) imports this
// instead of the package barrel, which carries CodeMirror.
export * from "./model/settings";
export { NOTEBOOK_SETTINGS_SCHEMA } from "./generated/notebookSettings";
