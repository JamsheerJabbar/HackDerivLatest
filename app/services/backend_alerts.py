"""Backend alerts for operators (not end users): unparsable LLM output, exhausted retries, ...

emit_backend_alert never raises. It logs, keeps a bounded in-memory history, and (when a Slack bot token is
configured) posts to settings.BACKEND_ALERT_CHANNEL, throttled per alert kind.
"""
import asyncio
import time
import uuid
from collections import Counter, deque
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional

from app.core.config import settings
from app.core.logger import logger

_DROP_KEYS = {"authorization", "token", "password", "secret", "jwt", "api_key"}
_history: Deque[Dict[str, Any]] = deque(maxlen=200)
_counts: Counter = Counter()
_last_slack: Dict[str, float] = {}


def _clean(ctx: Dict[str, Any]) -> Dict[str, Any]:
    out = {}
    for k, v in ctx.items():
        if str(k).lower() in _DROP_KEYS:
            continue
        try:
            out[k] = v if isinstance(v, (int, float, bool, type(None))) else str(v)[:500]
        except Exception:
            out[k] = "<unprintable>"
    return out


async def _post_alert_to_slack(text: str) -> bool:
    from app.services.slack_notifier import _post_to_slack
    return await _post_to_slack(settings.BACKEND_ALERT_CHANNEL, text)


def emit_backend_alert(kind: str, message: str, **context: Any) -> Dict[str, Any]:
    try:
        alert = {"id": str(uuid.uuid4()), "ts": datetime.now(timezone.utc).isoformat(), "kind": str(kind),
                 "message": str(message)[:500], "context": _clean(context)}
        _history.appendleft(alert)
        _counts[alert["kind"]] += 1
        logger.error(f"[BACKEND_ALERT] {alert['kind']}: {alert['message']} | {alert['context']}")
        now = time.monotonic()
        if settings.SLACK_BOT_TOKEN and now - _last_slack.get(alert["kind"], -1e9) >= settings.BACKEND_ALERT_SLACK_THROTTLE_SECONDS:
            _last_slack[alert["kind"]] = now
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(_safe_post(f":rotating_light: *{alert['kind']}* {alert['message']}"))
            except RuntimeError:
                pass  # no running loop: logged and stored only
        return alert
    except Exception as e:  # alerting must never break the request path
        try:
            logger.error(f"[BACKEND_ALERT] emit failed: {e}")
        except Exception:
            pass
        return {"id": None, "ts": None, "kind": str(kind), "message": str(message)[:500], "context": {}}


async def _safe_post(text: str) -> None:
    try:
        await _post_alert_to_slack(text)
    except Exception as e:
        logger.warning(f"Backend alert Slack post failed: {e}")


def recent_alerts(kind: Optional[str] = None) -> List[Dict[str, Any]]:
    return [a for a in _history if kind is None or a["kind"] == kind]


def alert_counts() -> Dict[str, int]:
    return dict(_counts)


def reset_alerts() -> None:
    _history.clear()
    _counts.clear()
    _last_slack.clear()
