# Copyright 2026 Marimo. All rights reserved.
# Modified by Alkera: import paths rewritten; see vendor/marimo/README.alkera.md
from __future__ import annotations

from alkera_notebook._marimo._data.data_source_discovery.helpers import (
    ENVIRONMENT_ORIGIN,
    environment_variable,
    has_value,
    hides_when_dialect,
)
from alkera_notebook._marimo._data.data_source_discovery.models import DetectedDataSource
from alkera_notebook._marimo._data.data_source_discovery.types import (
    DiscoveryContext,
    DiscoveryPlugin,
)


def discover(context: DiscoveryContext) -> list[DetectedDataSource]:
    if not has_value(context.environment, "SPARK_REMOTE"):
        return []

    return [
        DetectedDataSource(
            id="pyspark-connect-environment",
            integration="pyspark",
            category="database",
            display_name="PySpark (Spark Connect)",
            confidence="high",
            origins=(ENVIRONMENT_ORIGIN,),
            configuration=(environment_variable("Remote", "SPARK_REMOTE"),),
            hides_when=hides_when_dialect("pyspark", "spark"),
            code="""\
import os
import ibis
from pyspark.sql import SparkSession

session = SparkSession.builder.remote(
    os.environ["SPARK_REMOTE"]
).getOrCreate()
con = ibis.pyspark.connect(session)""",
        )
    ]


PYSPARK_PLUGIN = DiscoveryPlugin(id="pyspark", discover=discover)
