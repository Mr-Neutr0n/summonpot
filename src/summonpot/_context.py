"""Private compilation and admission for application-authenticated context.

Only explicit trusted Python calls or a server-owned HTTP request can supply context.
No context value is projected into model messages or public transport metadata.
"""

from __future__ import annotations

import inspect
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType, UnionType
from typing import Annotated, Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel, TypeAdapter

from summonpot._annotations import safe_get_type_hints
from summonpot._output_validation import _compile_output_validator


class AuthenticationError(PermissionError):
    """No application-authenticated principal is available for this request."""

    def __init__(self) -> None:
        super().__init__("Application authentication is required.")


class _ContextError(RuntimeError):
    """A trusted application provider violated its registered context contract."""

    def __init__(self) -> None:
        super().__init__("Application context did not satisfy its declared contract.")


@dataclass(frozen=True, slots=True)
class _HttpContext:
    request: Any = field(repr=False)


@dataclass(frozen=True, slots=True)
class _CompiledContext:
    provider: Callable[..., Any] = field(repr=False)
    model: type[BaseModel]
    validator: Any = field(repr=False)
    fields: Mapping[str, Any]


def _immutable_type(annotation: Any) -> bool:
    origin = get_origin(annotation)
    if origin is Annotated:
        return _immutable_type(get_args(annotation)[0])
    if origin is Union or origin is UnionType:
        return all(_immutable_type(arg) for arg in get_args(annotation))
    if origin is Literal:
        return all(_immutable_value(value) for value in get_args(annotation))
    if origin is tuple or origin is frozenset:
        return all(
            arg is Ellipsis or _immutable_type(arg) for arg in get_args(annotation)
        ) and bool(get_args(annotation))
    return any(
        annotation is kind for kind in (str, bytes, bool, int, float, type(None))
    )


def _immutable_value(value: Any, depth: int = 0) -> bool:
    if depth > 32:
        return False
    kind = type(value)
    if any(kind is expected for expected in (str, bytes, bool, int, type(None))):
        return True
    if kind is float:
        return math.isfinite(value)
    if kind is tuple or kind is frozenset:
        return len(value) <= 4096 and all(
            _immutable_value(item, depth + 1) for item in value
        )
    return False


def _compile_context(provider: Any) -> _CompiledContext | None:
    if provider is None:
        return None
    if not inspect.isfunction(provider):
        raise TypeError("context= requires a sync or async application function.")
    signature = inspect.signature(provider)
    parameters = tuple(signature.parameters.values())
    if len(parameters) != 1 or parameters[0].kind not in (
        inspect.Parameter.POSITIONAL_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
    ):
        raise TypeError("The context provider must accept exactly one HTTP request.")
    hints = safe_get_type_hints(provider)
    model = hints.get("return")
    if not isinstance(model, type) or not issubclass(model, BaseModel):
        raise TypeError("The context provider must declare a Pydantic return model.")
    if (
        model.model_config.get("frozen") is not True
        or model.model_config.get("extra") != "forbid"
    ):
        raise TypeError("Context models require frozen=True and extra='forbid'.")
    fields = {
        name: info.rebuild_annotation() for name, info in model.model_fields.items()
    }
    if not all(_immutable_type(annotation) for annotation in fields.values()):
        raise TypeError(
            "Context fields must use supported immutable scalar or tuple/frozenset types."
        )
    return _CompiledContext(
        provider,
        model,
        _compile_output_validator(TypeAdapter(model)),
        MappingProxyType(fields),
    )


async def _resolve_context(
    spec: _CompiledContext | None,
    supplied: Any,
    invoke: Callable[..., Any],
) -> Mapping[str, Any]:
    if spec is None:
        if supplied is not None:
            raise _ContextError()
        return MappingProxyType({})
    if type(supplied) is _HttpContext:
        # Keep application exception provenance separate from framework deadlines.
        from summonpot.runtime import _RuntimeDeadlineExceeded

        try:
            supplied = await invoke(spec.provider, supplied.request)
        except _RuntimeDeadlineExceeded:
            raise
        except PermissionError:
            raise AuthenticationError() from None
        except Exception:
            raise _ContextError() from None
    if supplied is None:
        raise AuthenticationError()
    if type(supplied) is not spec.model:
        raise _ContextError()
    try:
        validated = spec.validator.validate_python(supplied)
        storage = BaseModel.__dict__["__dict__"].__get__(validated, type(validated))
        if type(storage) is not dict or any(type(key) is not str for key in storage):
            raise _ContextError()
        values = {name: dict.__getitem__(storage, name) for name in spec.fields}
        if not all(_immutable_value(value) for value in values.values()):
            raise _ContextError()
    except Exception:
        raise _ContextError() from None
    # Every admitted value is immutable; this does not retain mutable principal storage.
    return MappingProxyType(values)
