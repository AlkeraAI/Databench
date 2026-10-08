"""Agent plugins: the framework (``plugin_base``) and the integrations built on it.

The framework discovers plugins through the ``CLI_PLUGINS`` extension point
(``alkera_cli.plugins.plugin_base.plugin``), never by importing them, so this
package imports nothing. The product's bundled data plugins are listed and registered by the
product's composition.
"""
