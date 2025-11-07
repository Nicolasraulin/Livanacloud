
import logging
import contextvars
from typing import Any, Dict, Optional

_ctx: contextvars.ContextVar[Dict[str, Any]] = contextvars.ContextVar("logctx", default={})

class ContextFilter(logging.Filter):
    """Injecte les champs du contexte dans chaque record."""
    def filter(self, record: logging.LogRecord) -> bool:
        ctx = _ctx.get()
        for k, v in ctx.items():
            if not hasattr(record, k):
                setattr(record, k, v)
        return True

def attach(logger: logging.Logger) -> logging.Logger:
    """Attache le filtre de contexte au logger racine """
    if not any(isinstance(f, ContextFilter) for f in logger.filters):
        logger.addFilter(ContextFilter())
    return logger

def bind(**fields: Any) -> None:
    """Ajoute/merge des champs au contexte courant (room, agent, user, )."""
    cur = dict(_ctx.get())
    cur.update({k: v for k, v in fields.items() if v is not None})
    _ctx.set(cur)

def unbind(*keys: str) -> None:
    """Retire certains champs du contexte."""
    cur = dict(_ctx.get())
    for k in keys:
        cur.pop(k, None)
    _ctx.set(cur)

def clear() -> None:
    """Reinitialise completement le contexte."""
    _ctx.set({})

def event(logger: logging.Logger, level: int, name: str, **fields: Any) -> None:
    """
    Petit helper pour des logs 'structures' (cle 'event' standard).
    Ex: event(logger, logging.INFO, "agent.start", room=..., agent=...)
    """
    logger.log(level, name, extra={"event": name, **fields})
