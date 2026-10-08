# `alkera_core.versioning`

The base class for every Pydantic model that is written to disk, or to any storage that outlives one process.

## When to use it

- **Use `VersionedModel`** for chat events, manifests, on-disk caches, anything persisted under `.alkera/`, and anything a later version of the writer must read back.
- **Use `pydantic.BaseModel`** for HTTP request and response shapes, RPC envelopes that live for one call, and anything where forward compatibility does not matter.

## What the base provides

1. **`extra="allow"`.** Unknown fields from a newer writer survive a round trip through an older reader. Never override it on a subclass.
2. **A `schema_version` field and a `SCHEMA_VERSION` class variable.** A semver string stamped on every persisted instance.
3. **A `MIGRATIONS` ladder**, keyed by the version migrated from. It runs as a `@model_validator(mode="before")`, so the dict is upgraded before fields are parsed.
4. **`@field_serializer("schema_version")`**, which always writes the current version, never the one that was read.
5. **`make_unknown_tag_discriminator(known_tags)`**, for tagged unions. It routes an unknown tag to a `RawX` fallback class, so an older reader never fails on a new variant.
6. **An `__init_subclass__` warning** when a concrete subclass does not set `SCHEMA_VERSION`. Intermediate bases opt out with `__abstract__: ClassVar[bool] = True`.

## Changing a subclass

The full rules are in "Versioned persisted models" in the repository's `AGENTS.md`. In short:

- **An additive change** (a new optional field with a default) is a minor bump, `1.0.0` to `1.1.0`. No migration is needed: older readers keep the field as an unknown extra, newer readers default it.
- **A rename, a removal or a new required field** is a major bump, `1.x` to `2.0.0`, with a migration in `MIGRATIONS[old_version]`.
- **Add a fixture** of the new writer's output under `packages/api-core/tests/fixtures/<model>/`. The lineage test loads every fixture with the current reader.
- **Never delete old fixtures or migration entries.**

## Pitfalls

- `model_dump(exclude_unset=True)` drops the extras that `extra="allow"` kept. Persist with `model_dump(mode="json")` and no exclude flag.
- JSON Schema output does not show `extra="allow"`. If the schema goes to an external validator, override `model_json_schema()` to add `additionalProperties: true`.
- `schema_version` is a `str`, never an `Enum`: an older reader would fail on an enum value it does not know.
- A migration is a pure function from dict to dict. No I/O, no database, no globals, so a test can replay it with no setup.

## Why not a library

No widely used library versions Pydantic models persisted to disk. [`cadwyn`](https://docs.cadwyn.dev/) versions FastAPI requests and responses, which is a different problem.
