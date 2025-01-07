"""
Transports for the Logger module for MCP Agent, including:
- Local + optional remote event transport
- Async event bus
"""

import asyncio

from typing import Dict, List, Protocol

import aiohttp
from opentelemetry import trace
from rich.console import Console
from rich.json import JSON
from rich.text import Text

from mcp_agent.config import LoggerSettings
from mcp_agent.logging.events import Event
from mcp_agent.logging.listeners import EventListener, LifecycleAwareListener


class EventTransport(Protocol):
    """
    Pluggable interface for sending events to a remote or external system
    (Kafka, RabbitMQ, REST, etc.).
    """

    async def send_event(self, event: Event):
        """
        Send an event to the external system.
        Args:
            event: Event to send.
        """
        ...


class NoOpTransport(EventTransport):
    """Default transport that does nothing (purely local)."""

    async def send_event(self, event):
        """Do nothing."""
        pass


class ConsoleTransport(EventTransport):
    """Simple transport that prints events to console."""

    def __init__(self):
        self.console = Console()
        self.log_level_styles: Dict[str, str] = {
            "info": "bold green",
            "debug": "dim white",
            "warning": "bold yellow",
            "error": "bold red",
        }

    async def send_event(self, event: Event):
        # Map log levels to styles
        style = self.log_level_styles.get(event.type, "white")

        # Create namespace without None
        namespace = event.namespace
        if event.name:
            namespace = f"{namespace}.{event.name}"

        log_text = Text.assemble(
            (f"[{event.type.upper()}] ", style),
            (f"{event.timestamp.isoformat()} ", "cyan"),
            (f"{namespace} ", "magenta"),
            (f"- {event.message}", "white"),
        )
        self.console.print(log_text)

        # Print additional data as a JSON if available
        if event.data:
            self.console.print(JSON.from_data(event.data))


class HTTPTransport(EventTransport):
    """
    Sends events to an HTTP endpoint in batches.
    Useful for sending to remote logging services like Elasticsearch, etc.
    """

    def __init__(
        self,
        endpoint: str,
        headers: Dict[str, str] = None,
        batch_size: int = 100,
        timeout: float = 5.0,
    ):
        self.endpoint = endpoint
        self.headers = headers or {}
        self.batch_size = batch_size
        self.timeout = timeout

        self.batch: List[Event] = []
        self.lock = asyncio.Lock()
        self._session: aiohttp.ClientSession | None = None

    async def start(self):
        """Initialize HTTP session."""
        if not self._session:
            self._session = aiohttp.ClientSession(
                headers=self.headers, timeout=aiohttp.ClientTimeout(total=self.timeout)
            )

    async def stop(self):
        """Close HTTP session and flush any remaining events."""
        if self.batch:
            await self._flush()
        if self._session:
            await self._session.close()
            self._session = None

    async def send_event(self, event: Event):
        """Add event to batch, flush if batch is full."""
        async with self.lock:
            self.batch.append(event)
            if len(self.batch) >= self.batch_size:
                await self._flush()

    async def _flush(self):
        """Send batch of events to HTTP endpoint."""
        if not self.batch:
            return

        if not self._session:
            await self.start()

        try:
            # Convert events to JSON-serializable dicts
            events_data = [
                {
                    "timestamp": event.timestamp.isoformat(),
                    "type": event.type,
                    "name": event.name,
                    "namespace": event.namespace,
                    "message": event.message,
                    "data": event.data,
                    "trace_id": event.trace_id,
                    "span_id": event.span_id,
                    "context": event.context.dict() if event.context else None,
                }
                for event in self.batch
            ]

            async with self._session.post(self.endpoint, json=events_data) as response:
                if response.status >= 400:
                    text = await response.text()
                    print(
                        f"Error sending log events to {self.endpoint}. "
                        f"Status: {response.status}, Response: {text}"
                    )
        except Exception as e:
            print(f"Error sending log events to {self.endpoint}: {e}")
        finally:
            self.batch.clear()


class AsyncEventBus:
    """
    Async event bus with local in-process listeners + optional remote transport.
    Also injects distributed tracing (trace_id, span_id) if there's a current span.
    """

    _instance = None

    def __init__(self, transport: EventTransport | None = None):
        self.transport: EventTransport = transport or NoOpTransport()
        self.listeners: Dict[str, EventListener] = {}
        self._queue = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._running = False
        self._stop_event = asyncio.Event()

    @classmethod
    def get(cls, transport: EventTransport | None = None) -> "AsyncEventBus":
        """Get the singleton instance of the event bus."""
        if cls._instance is None:
            cls._instance = cls(transport=transport)
        elif transport is not None:
            # Update transport if provided
            cls._instance.transport = transport
        return cls._instance

    async def start(self):
        """Start the event bus and all lifecycle-aware listeners."""
        if self._running:
            return

        # Start each lifecycle-aware listener
        for listener in self.listeners.values():
            if isinstance(listener, LifecycleAwareListener):
                await listener.start()

        # Clear stop event and start processing
        self._stop_event.clear()
        self._running = True
        self._task = asyncio.create_task(self._process_events())

    async def stop(self):
        """Stop the event bus and all lifecycle-aware listeners."""
        if not self._running:
            return

        # Signal processing to stop
        self._running = False
        self._stop_event.set()

        # Wait for queue to be processed
        if not self._queue.empty():
            try:
                await self._queue.join()
            except Exception:
                pass

        # Cancel and wait for task
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

        # Stop each lifecycle-aware listener
        for listener in self.listeners.values():
            if isinstance(listener, LifecycleAwareListener):
                await listener.stop()

    async def emit(self, event: Event):
        """Emit an event to all listeners and transport."""
        if not self._running:
            return

        # Inject current tracing info if available
        span = trace.get_current_span()
        if span.is_recording():
            ctx = span.get_span_context()
            event.trace_id = f"{ctx.trace_id:032x}"
            event.span_id = f"{ctx.span_id:016x}"

        # Forward to transport first (immediate processing)
        try:
            await self.transport.send_event(event)
        except Exception as e:
            print(f"Error in transport.send_event: {e}")

        # Then queue for listeners
        await self._queue.put(event)

    def add_listener(self, name: str, listener: EventListener):
        """Add a listener to the event bus."""
        self.listeners[name] = listener

    def remove_listener(self, name: str):
        """Remove a listener from the event bus."""
        self.listeners.pop(name, None)

    async def _process_events(self):
        """Process events from the queue until stopped."""
        while self._running:
            try:
                # Use wait_for with a timeout to allow checking running state
                try:
                    event = await asyncio.wait_for(self._queue.get(), timeout=0.1)
                except asyncio.TimeoutError:
                    continue

                # Process the event through all listeners
                tasks = []
                for listener in self.listeners.values():
                    try:
                        tasks.append(listener.handle_event(event))
                    except Exception as e:
                        print(f"Error creating listener task: {e}")

                if tasks:
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    for r in results:
                        if isinstance(r, Exception):
                            print(f"Error in listener: {r}")

                self._queue.task_done()

            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"Error in event processing loop: {e}")
                continue

        # Process remaining events in queue
        while not self._queue.empty():
            try:
                event = self._queue.get_nowait()
                tasks = []
                for listener in self.listeners.values():
                    try:
                        tasks.append(listener.handle_event(event))
                    except Exception:
                        pass
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
                self._queue.task_done()
            except asyncio.QueueEmpty:
                break


def create_transport(settings: LoggerSettings) -> EventTransport:
    """Create event transport based on settings."""
    if settings.type == "none":
        return NoOpTransport()
    elif settings.type == "console":
        return ConsoleTransport()
    elif settings.type == "http":
        if not settings.http_endpoint:
            raise ValueError("HTTP endpoint required for HTTP transport")
        return HTTPTransport(
            endpoint=settings.http_endpoint,
            headers=settings.http_headers,
            batch_size=settings.batch_size,
            timeout=settings.http_timeout,
        )
    else:
        raise ValueError(f"Unsupported transport type: {settings.type}")
