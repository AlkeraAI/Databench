import { render, screen } from "@testing-library/react";

import { downloadImage, imageBase64, imageDataUrl, imageFile, imageRenderer } from "./image";
import type { OutputContext } from "./types";

const PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==";

const context: OutputContext = { theme: "light", readonly: false, cellId: "c", outputId: "o", renderBundle: () => null };

function draw(mime: string, data: unknown, metadata?: Record<string, unknown>) {
  const Image = imageRenderer.Component;
  return render(<Image mime={mime} data={data} bundle={{ [mime]: data }} metadata={metadata} context={context} />);
}

describe("image renderer", () => {
  it.each(["image/png", "image/jpeg", "image/gif", "image/webp"])("draws %s as a data URL", (mime) => {
    draw(mime, PNG);
    expect(screen.getByRole("img")).toHaveAttribute("src", `data:${mime};base64,${PNG}`);
  });

  it("joins Jupyter's line-split base64 and drops whitespace", () => {
    draw("image/png", [PNG.slice(0, 20) + "\n", PNG.slice(20)]);
    expect(screen.getByRole("img")).toHaveAttribute("src", `data:image/png;base64,${PNG}`);
  });

  it("names the image for assistive tech, from metadata when given", () => {
    draw("image/png", PNG);
    expect(screen.getByRole("img", { name: "Output image" })).toBeInTheDocument();
  });

  it("takes alt and size from metadata, per MIME as Jupyter writes it", () => {
    draw("image/png", PNG, { alt: "A plot", "image/png": { width: 320, height: 200 } });
    const img = screen.getByRole("img", { name: "A plot" });
    expect(img).toHaveAttribute("width", "320");
    expect(img).toHaveAttribute("height", "200");
  });

  it.each([
    ["markup", '"><script>alert(1)</script>'],
    ["a URL", "https://tracker.example/p.png"],
    ["an object", { a: 1 }],
    ["empty", ""],
  ])("refuses a value that is not base64 (%s)", (_name, data) => {
    draw("image/png", data);
    expect(screen.queryByRole("img")).toBeNull();
    expect(screen.getByText("This image could not be read.")).toBeInTheDocument();
  });

  it("builds data URLs only for raster types", () => {
    expect(imageDataUrl("image/svg+xml", PNG)).toBeNull();
    expect(imageDataUrl("text/html", PNG)).toBeNull();
    expect(imageBase64("not base64!")).toBeNull();
  });
});

describe("downloadImage", () => {
  it("decodes the bytes into a typed file with the right extension", async () => {
    const file = imageFile("image/jpeg", "/9j/AA==", "plot");
    expect(file?.filename).toBe("plot.jpg");
    expect(file?.blob.type).toBe("image/jpeg");
    // jsdom's Blob has no arrayBuffer(); FileReader reads it.
    const buffer = await new Promise<ArrayBuffer>((resolve) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result as ArrayBuffer);
      reader.readAsArrayBuffer(file!.blob);
    });
    const bytes = new Uint8Array(buffer);
    expect([...bytes]).toEqual([0xff, 0xd8, 0xff, 0x00]);
  });

  it("clicks a download link for the file and cleans up", () => {
    const created: Blob[] = [];
    const createObjectURL = vi.fn((blob: Blob) => {
      created.push(blob);
      return "blob:fake";
    });
    vi.stubGlobal("URL", Object.assign(Object.create(URL), { createObjectURL, revokeObjectURL: () => {} }));
    const clicked: { href: string; download: string }[] = [];
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
      clicked.push({ href: this.getAttribute("href") ?? "", download: this.download });
    });
    try {
      expect(downloadImage("image/png", PNG, "chart")).toBe(true);
      expect(clicked).toEqual([{ href: "blob:fake", download: "chart.png" }]);
      expect(created[0].type).toBe("image/png");
      expect(document.querySelector("a[download]")).toBeNull();
      expect(downloadImage("image/svg+xml", PNG)).toBe(false);
      expect(clicked).toHaveLength(1);
    } finally {
      click.mockRestore();
      vi.unstubAllGlobals();
    }
  });
});
