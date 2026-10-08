"""The private extensions the product's backend installs at its composition root.

Each module here composes one private feature into the open app by registering
into ``backend.api.extension_points``. The open factory never imports this
package; the layers contract in ``apps/backend/.importlinter`` holds it to that.
"""
