// == ALKERA EDIT START — tests for the secrets-by-file reader (whole file is alkera-local).
import { afterEach, describe, expect, test } from "bun:test"
import { existsSync, mkdtempSync, writeFileSync } from "node:fs"
import os from "node:os"
import path from "node:path"
import { alkeraSecret, resetAlkeraSecretsForTests } from "../src/alkera-secrets"

const NAMES = ["ALKERA_SECRETS_FILE", "ALKERA_CONFIG_CONTENT", "ALKERA_SERVER_PASSWORD", "ALKERA_OTHER"]

function secretsFile(body: string): string {
  const file = path.join(mkdtempSync(path.join(os.tmpdir(), "alkera-secrets-")), "secrets.json")
  writeFileSync(file, body, { mode: 0o600 })
  return file
}

afterEach(() => {
  for (const name of NAMES) delete process.env[name]
  resetAlkeraSecretsForTests()
})

describe("alkeraSecret", () => {
  test("reads the named file once, removes it, and forgets where it was", () => {
    const file = secretsFile(JSON.stringify({ ALKERA_SERVER_PASSWORD: "pw", ALKERA_CONFIG_CONTENT: "{}" }))
    process.env["ALKERA_SECRETS_FILE"] = file

    expect(alkeraSecret("ALKERA_SERVER_PASSWORD")).toBe("pw")
    expect(existsSync(file)).toBe(false)
    expect(process.env["ALKERA_SECRETS_FILE"]).toBeUndefined()
    expect(alkeraSecret("ALKERA_CONFIG_CONTENT")).toBe("{}")
  })

  test("the file's value wins over the environment's", () => {
    process.env["ALKERA_SERVER_PASSWORD"] = "from-env"
    process.env["ALKERA_SECRETS_FILE"] = secretsFile(JSON.stringify({ ALKERA_SERVER_PASSWORD: "from-file" }))

    expect(alkeraSecret("ALKERA_SERVER_PASSWORD")).toBe("from-file")
  })

  test("takes only the names it hands out, and only strings", () => {
    process.env["ALKERA_SECRETS_FILE"] = secretsFile(
      JSON.stringify({ ALKERA_OTHER: "smuggled", ALKERA_SERVER_PASSWORD: 7 }),
    )

    expect(alkeraSecret("ALKERA_OTHER")).toBeUndefined()
    expect(alkeraSecret("ALKERA_SERVER_PASSWORD")).toBeUndefined()
  })

  test("a named file that cannot be read stops every caller, never falling back to the environment", () => {
    process.env["ALKERA_SERVER_PASSWORD"] = "from-env"
    process.env["ALKERA_SECRETS_FILE"] = path.join(os.tmpdir(), "alkera-secrets-missing", "secrets.json")

    expect(() => alkeraSecret("ALKERA_SERVER_PASSWORD")).toThrow()
    expect(() => alkeraSecret("ALKERA_SERVER_PASSWORD")).toThrow()
  })

  test("a named file that is not JSON stops the caller", () => {
    process.env["ALKERA_SECRETS_FILE"] = secretsFile("not json")

    expect(() => alkeraSecret("ALKERA_SERVER_PASSWORD")).toThrow()
  })

  test("without a file the environment is read as upstream does", () => {
    process.env["ALKERA_SERVER_PASSWORD"] = "from-env"

    expect(alkeraSecret("ALKERA_SERVER_PASSWORD")).toBe("from-env")
  })
})
// == ALKERA EDIT END
