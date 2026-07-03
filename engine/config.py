"""Configuration and environment loading for sharpe-engine.

Two responsibilities, deliberately kept in one small module:

1. **Tuneable parameters** — :func:`load_settings` reads ``config/settings.yaml``
   into an attribute-accessible object so callers write ``settings.universe
   .min_price`` rather than threading dicts around. Nothing in source code
   hardcodes a parameter that belongs in the YAML (CLAUDE.md constraint).

2. **Secrets** — :func:`load_env` populates ``os.environ`` from ``.env.paper``
   or ``.env.live`` via python-dotenv, and :func:`get_alpaca_client` builds the
   read client from those variables. Credentials are *only* ever read from the
   environment; they never appear in source or settings.yaml.

Heavy / optional imports (``yaml``, ``dotenv``, the Alpaca SDK) are imported
lazily inside the functions that need them so that importing this module — and
the pure pipeline code that depends on it — stays cheap and test-friendly.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

_DEFAULT_SETTINGS_PATH = Path("config/settings.yaml")
_VALID_ENVS = ("paper", "live")


def load_settings(path: str | Path = _DEFAULT_SETTINGS_PATH) -> SimpleNamespace:
    """Load ``settings.yaml`` into a nested, attribute-accessible namespace.

    Raises:
        FileNotFoundError: If the settings file does not exist.
    """
    import yaml

    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"settings file not found: {path}")
    with path.open("r") as fh:
        data = yaml.safe_load(fh) or {}
    return _to_namespace(data)


def _to_namespace(obj: object) -> object:
    """Recursively convert dicts to SimpleNamespace; leave other types intact."""
    if isinstance(obj, dict):
        return SimpleNamespace(**{k: _to_namespace(v) for k, v in obj.items()})
    if isinstance(obj, list):
        return [_to_namespace(v) for v in obj]
    return obj


def load_env(env: str = "paper") -> None:
    """Load ``.env.<env>`` into ``os.environ``.

    Args:
        env: ``"paper"`` (default) or ``"live"``.

    Raises:
        ValueError: If ``env`` is not ``"paper"`` or ``"live"``.
        FileNotFoundError: If the corresponding ``.env.<env>`` file is absent.
    """
    if env not in _VALID_ENVS:
        raise ValueError(f"env must be one of {_VALID_ENVS}, got {env!r}")
    from dotenv import load_dotenv

    env_path = Path(f".env.{env}")
    if not env_path.exists():
        raise FileNotFoundError(f"environment file not found: {env_path}")
    load_dotenv(env_path, override=True)


def require_env(name: str) -> str:
    """Return ``os.environ[name]`` or raise if missing/empty.

    Guards against silently running with unset credentials.
    """
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(
            f"required environment variable {name!r} is not set "
            f"(load .env.paper / .env.live first)"
        )
    return value


def get_alpaca_client(feed: str = "iex"):
    """Build the read :class:`AlpacaClient` from environment credentials.

    Imports the client lazily so modules that only need pure pipeline helpers
    do not pull in the Alpaca SDK.

    Raises:
        RuntimeError: If ALPACA_API_KEY / ALPACA_SECRET_KEY are not set.
    """
    from engine.alpaca_client import AlpacaClient

    return AlpacaClient(
        api_key=require_env("ALPACA_API_KEY"),
        secret_key=require_env("ALPACA_SECRET_KEY"),
        feed=feed,
    )


def is_paper_env() -> bool:
    """Whether the loaded environment targets Alpaca's **paper** API.

    Derived from ``ALPACA_BASE_URL`` (the one place the env already declares it) instead of a
    hardcoded ``paper=True`` scattered across clients — the audit's Phase-7 landmine: with
    ``--env live`` the trading stream would have silently connected to paper. Unset/blank
    defaults to paper (the safe side).
    """
    url = os.environ.get("ALPACA_BASE_URL", "").strip().lower()
    return ("paper" in url) if url else True


def get_trading_stream():
    """Build an Alpaca ``TradingStream`` from env credentials — the live order feed's
    websocket source (see :class:`engine.order_feed.LiveOrderFeed`). Paper/live follows
    :func:`is_paper_env`. Imported lazily so pure pipeline code never pulls the streaming SDK.

    Raises:
        RuntimeError: If ALPACA_API_KEY / ALPACA_SECRET_KEY are not set.
    """
    from alpaca.trading.stream import TradingStream

    return TradingStream(require_env("ALPACA_API_KEY"), require_env("ALPACA_SECRET_KEY"),
                         paper=is_paper_env())


def get_stock_data_stream(feed: str = "iex"):
    """Build an Alpaca ``StockDataStream`` — the live market-data source behind the dashboard's
    sub-second headline metrics (:class:`engine.price_feed.LivePriceFeed`). Defaults to the free
    IEX feed. Imported lazily.

    Raises:
        RuntimeError: If ALPACA_API_KEY / ALPACA_SECRET_KEY are not set.
    """
    from alpaca.data.enums import DataFeed
    from alpaca.data.live import StockDataStream

    return StockDataStream(require_env("ALPACA_API_KEY"), require_env("ALPACA_SECRET_KEY"),
                           feed=DataFeed(feed))
