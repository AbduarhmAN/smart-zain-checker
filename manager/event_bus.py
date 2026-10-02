"""In-Memory Event Bus for Smart Zain Checker.
Provides loose coupling across the entire system (Workers, UI, Telegram, Exporters).
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Dict, List

logger = logging.getLogger("EventBus")


class EventBus:
    """Thread-safe publish/subscribe event dispatcher."""

    def __init__(self) -> None:
        self._subscribers: Dict[str, List[Callable[[str, Any], None]]] = {}
        self._lock = threading.Lock()

    def subscribe(self, event_type: str, callback: Callable[[str, Any], None]) -> None:
        """Subscribes a callback to a specific event type, or '*' for all events."""
        with self._lock:
            if event_type not in self._subscribers:
                self._subscribers[event_type] = []
            self._subscribers[event_type].append(callback)

    def unsubscribe(self, event_type: str, callback: Callable[[str, Any], None]) -> None:
        with self._lock:
            if event_type in self._subscribers:
                self._subscribers[event_type] = [cb for cb in self._subscribers[event_type] if cb != callback]

    def publish(self, event_type: str, payload: Any = None) -> None:
        """Publishes an event to all interested subscribers concurrently or safely."""
        with self._lock:
            callbacks = list(self._subscribers.get(event_type, []))
            all_callbacks = list(self._subscribers.get("*", []))

        combined = callbacks + all_callbacks
        for cb in combined:
            try:
                cb(event_type, payload)
            except Exception as exc:
                logger.error(f"Error in EventBus subscriber for event '{event_type}': {exc}", exc_info=True)


# Global singleton instance
EVENT_BUS = EventBus()
