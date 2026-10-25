"""Drive the on-device LLM that Google Chrome downloads, from Python.

Chrome ships and runs the model; this package talks to it through the browser's
built-in Prompt API over the DevTools Protocol, so inference stays on-device.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
