"""
title: Inference Snap Auto-Discovery
description: Scans a range of local ports to dynamically discover and route to inference snaps.
version: 0.1.0
"""
import asyncio
import json
import time

import httpx
from pydantic import BaseModel, Field

API_VERSIONS = ("v3", "v1")
BASE_HOST = "127.0.0.1"


def endpoint_url(port: int, api_version: str) -> str:
    return f"http://{BASE_HOST}:{port}/{api_version}"


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
        PORT_RANGES: str = Field(
            default="8324-8400",
            description="Local ports scanned for inference snaps, as a "
                        "comma-separated list of ports or port ranges.",
        )
        API_KEY: str = Field(
            default="-",
            description="API key sent to the discovered endpoints. Only needed if a "
                        "backend requires authentication.",
        )
        REQUEST_TIMEOUT: float = Field(
            default=600.0,
            description="Time (seconds) before a chat completion times out. Keep it "
                        "generous: large contexts (e.g. RAG) are slow on CPU-only backends.",
        )
        CONNECT_TIMEOUT: float = Field(
            default=10.0,
            description="Time (seconds) before a selected snap is deemed unreachable. "
                        "Keep it short so a stopped snap (closed port) fails fast.",
        )
        DISCOVERY_TIMEOUT: float = Field(
            default=0.5,
            description="Time (seconds) before a port is deemed to have no snap "
                        "listening during discovery.",
        )
        DISCOVERY_CONCURRENCY: int = Field(
            default=32,
            description="Number of ports probed in parallel during discovery.",
        )
        DISCOVERY_CACHE_TTL: float = Field(
            default=30.0,
            description="Time (seconds) discovery results are reused before the ports "
                        "are scanned again. Set to 0 to disable caching.",
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

        headers = {"Authorization": f"Bearer {self.valves.API_KEY}"}
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
                endpoint = endpoint_url(port, api_version)
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
                        "id": f"{port}|{api_version}|{model['id']}",
                        "name": f"Snap: {model['id']} (Port {port})",
                    }
                    for model in data
                    if isinstance(model, dict) and model.get("id")
                ]
        return []

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------
    def _resolve_endpoint_and_model_id(self, model_id_full: str):
        """Decode a model id into ``(endpoint URL, snap model id)``.

        The model ID field is repurposed to carry the port and API version of the snap, so that the
        routing logic can determine which local endpoint to send the request to. The format is:
        ``<port>|<api_version>|<model_id>``

        This function extracts the port and API version, validates them, and constructs the endpoint URL.
        If the model ID is malformed or points to an unknown endpoint, it raises a ValueError.
        """
        port_field, _, remainder = model_id_full.partition("|")
        api_version, separator, model_id = remainder.partition("|")
        if not separator or not model_id:
            raise ValueError(f"Could not determine routing URL from model id: {model_id_full}")

        try:
            port = int(port_field.rsplit(".", 1)[-1])
        except ValueError:
            raise ValueError(f"Could not determine routing URL from model id: {model_id_full}")

        if api_version not in API_VERSIONS:
            raise ValueError(
                f"Refusing to route model id with an unknown API version "
                f"{api_version!r}: {model_id_full}"
            )

        if port not in set(parse_ports(self.valves.PORT_RANGES)):
            raise ValueError(
                f"Refusing to route model id to port {port}, which is outside the "
                f"configured PORT_RANGES: {model_id_full}"
            )

        return endpoint_url(port, api_version), model_id

    async def pipe(self, body: dict):
        endpoint, original_model_id = self._resolve_endpoint_and_model_id(body.get("model", ""))

        payload = {**body, "model": original_model_id}
        payload.pop("user", None)
        payload.pop("chat_id", None)
        payload.pop("title", None)

        headers = {"Authorization": f"Bearer {self.valves.API_KEY}"}

        timeout = httpx.Timeout(
            self.valves.REQUEST_TIMEOUT,
            connect=self.valves.CONNECT_TIMEOUT,
        )

        if payload.get("stream", False):
            # Do NOT use 'async with' here. Return the generator directly.
            return self._stream_response(endpoint, headers, payload, timeout)
        else:
            # Sync requests can still use the context manager safely
            async with httpx.AsyncClient(headers=headers) as client:
                return await self._sync_response(client, endpoint, payload, timeout)

    async def _sync_response(self, client, endpoint, payload, timeout):
        response = await client.post(f"{endpoint}/chat/completions", json=payload, timeout=timeout)
        response.raise_for_status()
        # A backend that answers 200 with a body that is not an OpenAI chat
        # completion must fail this request only, with a message that names the
        # offending endpoint, rather than surfacing a bare KeyError/IndexError.
        try:
            return response.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ValueError(
                f"Unexpected chat completion response from {endpoint}: {exc}"
            ) from exc

    async def _stream_response(self, endpoint, headers, payload, timeout):
        # Instantiate the client manually inside the generator function
        client = httpx.AsyncClient(headers=headers)

        try:
            async with client.stream("POST", f"{endpoint}/chat/completions", json=payload,
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
                        except (json.JSONDecodeError, KeyError, IndexError, TypeError, AttributeError) as e:
                            # A malformed chunk is skipped rather than aborting an
                            # otherwise usable stream; transport/HTTP failures are
                            # still raised above.
                            print(f"DEBUG: Stream parse error: {e} on data: {data_str}")
        finally:
            # Explicitly close the client when the generator is finished or errors out
            await client.aclose()
