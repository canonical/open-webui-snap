"""
title: Snap Model Auto-Discovery
description: Scans a range of local ports to dynamically discover and route to LLM snaps with corrected streaming.
version: 0.1.0
"""
import json

import httpx
from pydantic import BaseModel, Field


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
    return sorted(list(ports))


class Pipe:
    class Valves(BaseModel):
        PORT_RANGES: str = Field(default="8324-8400", description="Comma-separated list of ports.")
        DUMMY_API_KEY: str = Field(default="-", description="Dummy API key.")

    def __init__(self):
        self.type = "manifold"
        self.valves = self.Valves()

    async def pipes(self):
        models = []
        ports = parse_ports(self.valves.PORT_RANGES)
        headers = {"Authorization": f"Bearer {self.valves.DUMMY_API_KEY}"}

        async with httpx.AsyncClient(headers=headers) as client:
            for port in ports:
                base_url = f"http://127.0.0.1:{port}"
                for api_version in ["/v3", "/v1"]:
                    endpoint = f"{base_url}{api_version}"
                    try:
                        res = await client.get(f"{endpoint}/models", timeout=0.5)
                        if res.status_code == 200:
                            for model in res.json().get("data", []):
                                models.append({
                                    "id": f"{endpoint}|{model['id']}",
                                    "name": f"Snap: {model['id']} (Port {port})"
                                })
                            break
                    except Exception:
                        continue
        return models

    async def pipe(self, body: dict):
        model_id_full = body.get("model", "")

        encoded_part = model_id_full.split(".", 1)[-1] if "." in model_id_full else model_id_full

        try:
            endpoint_url, original_model_id = encoded_part.split("|", 1)
        except ValueError:
            return f"Error: Could not determine routing URL from: {model_id_full}"

        payload = {**body, "model": original_model_id}
        payload.pop("user", None)
        payload.pop("chat_id", None)
        payload.pop("title", None)

        headers = {"Authorization": f"Bearer {self.valves.DUMMY_API_KEY}"}

        if payload.get("stream", False):
            # Do NOT use 'async with' here. Return the generator directly.
            return self._stream_response(endpoint_url, headers, payload)
        else:
            # Sync requests can still use the context manager safely
            async with httpx.AsyncClient(headers=headers) as client:
                return await self._sync_response(client, endpoint_url, payload)

    async def _sync_response(self, client, endpoint_url, payload):
        response = await client.post(f"{endpoint_url}/chat/completions", json=payload, timeout=120.0)
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]

    async def _stream_response(self, endpoint_url, headers, payload):
        # Instantiate the client manually inside the generator function
        client = httpx.AsyncClient(headers=headers)

        try:
            async with client.stream("POST", f"{endpoint_url}/chat/completions", json=payload,
                                     timeout=120.0) as response:
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
        except Exception as e:
            error_msg = f"\n[Stream Error: {str(e)}]"
            print(error_msg)
            yield error_msg
        finally:
            # Explicitly close the client when the generator is finished or errors out
            await client.aclose()