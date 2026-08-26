"""
title: Snap Model Auto-Discovery
description: Scans a range of local ports to dynamically discover and route to LLM snaps with corrected streaming.
version: 0.1.0
"""
import asyncio
import json
import time
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field

# API versions probed on each candidate port, in preference order.
API_VERSIONS = ("/v3", "/v1")
# Inference snaps only ever listen on the loopback interface.
ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def parse_ports(port_string: str) -> list[int]:
    ports = set()
    for part in port_string.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            try:
                start, end = map(int, part.split("-"))
                ports.update(range(start, end + 1))
            except ValueError:
                pass
        else:
            try:
                ports.add(int(part))
            except ValueError:
                pass
    return sorted(p for p in ports if 1 <= p <= 65535)


class Pipe:
    class Valves(BaseModel):
        PORT_RANGES: str = Field(default="8324-8400", description="Comma-separated list of ports or port ranges.")
        DUMMY_API_KEY: str = Field(default="-", description="Dummy API key.")
        # Maximum allowed delay before a chat completion request towards an inference snap times out
        REQUEST_TIMEOUT: float = Field(
            default=600.0,
            description="Read/write timeout (seconds) for chat completions. Large "
            "contexts (e.g. RAG) can take a long time to prefill and generate on "
            "CPU-only backends, so keep this generous.",
        )
        # Maximum allowed delay before a snap is deemed unreachable when selected by the user for inference
        CONNECT_TIMEOUT: float = Field(
            default=10.0,
            description="Connection timeout (seconds). Kept short so a stopped snap "
            "(closed port) fails fast instead of blocking.",
        )
        DISCOVERY_TIMEOUT: float = Field(
            default=0.5,
            description="Per-probe timeout (seconds) when scanning ports for models.",
        )
        DISCOVERY_CONCURRENCY: int = Field(
            default=32,
            description="Number of ports probed in parallel during discovery.",
        )
        DISCOVERY_CACHE_TTL: float = Field(
            default=30.0,
            description="How long (seconds) discovery results are reused before the "
            "port range is scanned again. Set to 0 to disable caching.",
        )

    def __init__(self):
        self.type = "manifold"
        self.valves = self.Valves()
        self._cache = None
        self._cache_expiry = 0.0
        self._cache_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------
    async def pipes(self):
        ttl = self.valves.DISCOVERY_CACHE_TTL
        if ttl > 0 and self._cache is not None and time.monotonic() < self._cache_expiry:
            return self._cache

        async with self._cache_lock:
            # Another caller may have refreshed the cache while we waited.
            if ttl > 0 and self._cache is not None and time.monotonic() < self._cache_expiry:
                return self._cache

            models = await self._discover()
            self._cache = models
            self._cache_expiry = time.monotonic() + ttl
            return models

    async def _discover(self) -> list[dict]:
        ports = parse_ports(self.valves.PORT_RANGES)
        if not ports:
            return []

        headers = {"Authorization": f"Bearer {self.valves.DUMMY_API_KEY}"}
        concurrency = max(1, int(self.valves.DISCOVERY_CONCURRENCY))
        semaphore = asyncio.Semaphore(concurrency)
        limits = httpx.Limits(max_connections=concurrency)

        async with httpx.AsyncClient(headers=headers, limits=limits) as client:
            results = await asyncio.gather(
                *(self._probe_port(client, semaphore, port) for port in ports)
            )

        models = []
        for port_models in results:
            models.extend(port_models)
        return models

    async def _probe_port(self, client, semaphore, port: int) -> list[dict]:
        """Probe one port for an OpenAI-compatible model list.

        Every probe is bounded by DISCOVERY_TIMEOUT (including the case where the
        port accepts the connection but never answers), so a single unresponsive
        snap cannot stall the whole model list.
        """
        timeout = self.valves.DISCOVERY_TIMEOUT
        async with semaphore:
            for api_version in API_VERSIONS:
                endpoint = f"http://127.0.0.1:{port}{api_version}"
                try:
                    res = await asyncio.wait_for(
                        client.get(f"{endpoint}/models", timeout=timeout),
                        timeout=max(timeout, 0.1) * 2,
                    )
                except Exception:
                    continue
                if res.status_code != 200:
                    continue
                try:
                    data = res.json().get("data", [])
                except ValueError:
                    continue
                return [
                    {
                        "id": f"{endpoint}|{model['id']}",
                        "name": f"Snap: {model['id']} (Port {port})",
                    }
                    for model in data
                    if isinstance(model, dict) and model.get("id")
                ]
        return []

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------
    def _normalise_endpoint(self, endpoint_url: str):
        """Return a safe, rebuilt endpoint URL, or None if it is not allowed.

        The model id is caller-controlled, so the encoded endpoint must be
        re-validated here: only loopback HTTP endpoints on a configured port and
        a known API version path may ever be contacted.
        """
        try:
            parsed = urlparse(endpoint_url)
        except ValueError:
            return None

        if parsed.scheme != "http":
            return None
        if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.params:
            return None
        try:
            host = parsed.hostname
            port = parsed.port
        except ValueError:
            return None
        if host is None or host.lower() not in ALLOWED_HOSTS:
            return None
        if port is None or port not in set(parse_ports(self.valves.PORT_RANGES)):
            return None
        if parsed.path not in API_VERSIONS:
            return None

        return f"http://127.0.0.1:{port}{parsed.path}"

    async def pipe(self, body: dict):
        model_id_full = body.get("model", "")

        encoded_part = model_id_full.split(".", 1)[-1] if "." in model_id_full else model_id_full

        try:
            endpoint_url, original_model_id = encoded_part.split("|", 1)
        except ValueError:
            raise ValueError(f"Could not determine routing URL from model id: {model_id_full}")

        endpoint_url = self._normalise_endpoint(endpoint_url)
        if endpoint_url is None or not original_model_id:
            raise ValueError(
                f"Refusing to route model id to a non-local inference endpoint: {model_id_full}"
            )

        payload = {**body, "model": original_model_id}
        payload.pop("user", None)
        payload.pop("chat_id", None)
        payload.pop("title", None)

        headers = {"Authorization": f"Bearer {self.valves.DUMMY_API_KEY}"}

        timeout = httpx.Timeout(
            self.valves.REQUEST_TIMEOUT,
            connect=self.valves.CONNECT_TIMEOUT,
        )

        if payload.get("stream", False):
            # Do NOT use 'async with' here. Return the generator directly.
            return self._stream_response(endpoint_url, headers, payload, timeout)
        else:
            # Sync requests can still use the context manager safely
            async with httpx.AsyncClient(headers=headers) as client:
                return await self._sync_response(client, endpoint_url, payload, timeout)

    async def _sync_response(self, client, endpoint_url, payload, timeout):
        response = await client.post(f"{endpoint_url}/chat/completions", json=payload, timeout=timeout)
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]

    async def _stream_response(self, endpoint_url, headers, payload, timeout):
        # Instantiate the client manually inside the generator function
        client = httpx.AsyncClient(headers=headers)

        try:
            async with client.stream("POST", f"{endpoint_url}/chat/completions", json=payload,
                                     timeout=timeout) as response:
                # Upstream/transport failures must surface as errors rather than
                # being yielded as ordinary assistant content, otherwise a failed
                # inference looks like a successful completion to the client.
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        data_str = line[6:].strip()
                        if data_str == "[DONE]":
                            break
                        try:
                            data = json.loads(data_str)
                            delta = data["choices"][0]["delta"].get("content", "")
                            if delta:
                                yield delta
                        except (json.JSONDecodeError, KeyError) as e:
                            print(f"DEBUG: Stream parse error: {e} on data: {data_str}")
        finally:
            # Explicitly close the client when the generator is finished or errors out
            await client.aclose()
