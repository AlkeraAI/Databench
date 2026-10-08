/* The build scopes a module's class names, so the import resolves to a map from
 * the short role word in the stylesheet to the emitted name. Only `.module.css`
 * gets this shape; a plain `.css` import is a side effect and returns nothing. */
declare module "*.module.css" {
  const classes: Readonly<Record<string, string>>;
  export default classes;
}
