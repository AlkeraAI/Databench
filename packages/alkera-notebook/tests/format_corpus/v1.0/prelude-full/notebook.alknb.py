#!/usr/bin/env -S uv run --script
# -*- coding: utf-8 -*-
# /// script
# requires-python = ">=3.11"
# dependencies = ["polars>=1.9"]
# ///
# >>> alkera
# format = "1.0"
# dataframe = "polars"
# <<< alkera
# Copyright 2026 Example Co.
# SPDX-License-Identifier: Apache-2.0
"""Weekly revenue, by region."""

import marimo

__generated_with = "0.25.1"
app = marimo.App()

with app.setup(alkera_id="0000000001"):
    import alkera
    import polars as pl





if __name__ == "__main__":
    app.run()
