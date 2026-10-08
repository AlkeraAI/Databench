// The strict Plotly bundle ships without types; these are the calls the frame
// module makes.
declare module "plotly.js-strict-dist-min" {
  const Plotly: {
    newPlot(root: HTMLElement, data: unknown[], layout?: Record<string, unknown>, config?: Record<string, unknown>): Promise<HTMLElement>;
    relayout(root: HTMLElement, update: Record<string, unknown>): Promise<HTMLElement>;
  };
  export default Plotly;
}
