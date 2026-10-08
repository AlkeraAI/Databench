"""Customer-declared producer-to-Kafka-to-consumer lineage contracts."""

from __future__ import annotations

from typing import Annotated, ClassVar

from pydantic import Field, StringConstraints, model_validator

from alkera_core.versioning import VersionedModel

NonEmptyString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class KafkaFieldMap(VersionedModel):
    """One explicit producer field to topic field to consumer field mapping."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    producer: NonEmptyString
    topic: NonEmptyString
    consumer: NonEmptyString


class KafkaContract(VersionedModel):
    """One connection-bound stream edge with an explicit field-identity claim."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    connection: NonEmptyString
    topic: NonEmptyString
    producer: NonEmptyString
    consumer: NonEmptyString
    field_maps: list[KafkaFieldMap] = Field(default_factory=list)
    identity: bool = False

    @model_validator(mode="after")
    def _require_one_mapping_mode(self) -> KafkaContract:
        """Require exactly one of explicit field maps or declared identity behavior."""
        if bool(self.field_maps) == self.identity:
            raise ValueError("set either field_maps or identity: true, but not both")
        return self


class KafkaContracts(VersionedModel):
    """The versioned root document loaded from ``kafka-contracts.yml``."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    contracts: list[KafkaContract] = Field(min_length=1)


__all__ = ["KafkaContract", "KafkaContracts", "KafkaFieldMap"]
