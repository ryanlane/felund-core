# MVP API Quickstart

This quickstart runs the first Felund MVP rendezvous API implementation.

## Location

- Service file: `api/rendezvous.py`
- Dependencies: `api/requirements.txt`

## Start the service

From repo root:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r api/requirements.txt
uvicorn api.rendezvous:app --host 0.0.0.0 --port 8080 --reload
```

Health check:

```bash
curl http://127.0.0.1:8080/v1/health
```

## MVP Endpoints

- `POST /v1/register`
- `GET /v1/peers?circle_hint=...&limit=...`
- `DELETE /v1/register`
- `GET /v1/health`

## Example Calls

Register:

```bash
curl -X POST http://127.0.0.1:8080/v1/register \
  -H 'content-type: application/json' \
  -H 'X-Felund-Node: node-abc' \
  -d '{
    "node_id": "node-abc",
    "circle_hint": "f8d0ca8e62e98fdc",
    "endpoints": [
      {"transport":"tcp","host":"203.0.113.10","port":9999,"family":"ipv4","nat":"unknown"}
    ],
    "capabilities": {"relay": false, "transport": ["tcp"]},
    "ttl_s": 120
  }'
```

Lookup peers:

```bash
curl 'http://127.0.0.1:8080/v1/peers?circle_hint=f8d0ca8e62e98fdc&limit=20' \
  -H 'X-Felund-Node: node-xyz'
```

Unregister:

```bash
curl -X DELETE http://127.0.0.1:8080/v1/register \
  -H 'content-type: application/json' \
  -d '{"node_id": "node-abc", "circle_hint": "f8d0ca8e62e98fdc"}'
```

## Notes

- Current storage is in-memory only.
- Data disappears on process restart.
- Signature verification from the spec is not yet enforced in this scaffold.
- Relay endpoints are not implemented yet in this scaffold.

## Enable in chat client (feature flag)

The interactive chat client can use this API for periodic register + peer discovery.

Linux/macOS:

```bash
export FELUND_API_BASE=http://127.0.0.1:8080
python chat/felundchat.py
```

Windows PowerShell:

```powershell
$env:FELUND_API_BASE = "http://127.0.0.1:8080"
python chat/felundchat.py
```

When enabled, the client logs:

- `[api] rendezvous enabled: ...`
