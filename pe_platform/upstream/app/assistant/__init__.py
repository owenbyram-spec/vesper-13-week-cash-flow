"""
app/assistant/__init__.py
─────────────────────────
Loads the router when requested, preserving `from app.assistant import router`
without initializing application routes when only tool schemas are imported.
"""
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .main import router

__all__ = ["router"]
_EXPORT_MODULES = {"router": ".main"}


def __getattr__(name: str):
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name, __name__), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
